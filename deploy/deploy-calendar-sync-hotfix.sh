#!/usr/bin/env bash
# Root-only calendar sync rollout; no dependency or environment changes.
# Usage: bash deploy/deploy-calendar-sync-hotfix.sh <validated-full-commit-sha>
set +x
set -euo pipefail
umask 077

APP=/home/django/app
APP_USER=django
BASE=8323bb9ca402a5c0d25628855c5b23e36de64229
BRANCH=fix/beta-calendar-sync-2026-10-28
ORIGIN=https://rytivo.app
CGROUP_ROOT=/sys/fs/cgroup
TARGET="${1:-}"

fail() { echo "$*" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || fail 'Run this helper as root.'
[[ $# -eq 1 && $TARGET =~ ^[0-9a-f]{40}$ ]] || fail 'Supply exactly one validated, full commit SHA.'

# Checkout replaces this file. Continue from an immutable private copy.
if [[ ${REPBASE_CALENDAR_SYNC_PINNED:-} != 1 ]]; then
    pinned="$(mktemp /tmp/repbase-calendar-sync.XXXXXXXX)"
    trap 'rm -f -- "$pinned"' EXIT
    cp -- "$0" "$pinned"
    chmod 700 "$pinned"
    REPBASE_CALENDAR_SYNC_PINNED=1 bash "$pinned" "$TARGET"
    exit $?
fi

[[ -d $APP/.git && -x $APP/.venv/bin/python ]] || fail 'Expected backend checkout/virtualenv is missing.'
[[ -r /etc/repbase.env ]] || fail 'Existing backend environment file is missing.'
for command in git sudo flock systemctl pg_dump pg_restore sha256sum curl cmp realpath; do
    command -v "$command" >/dev/null || fail "Required command is unavailable: $command"
done
exec 9>/run/lock/repbase-deploy.lock
flock -n 9 || fail 'Another deployment holds the deployment lock.'

git_app() { sudo -H -u "$APP_USER" git -C "$APP" "$@"; }
clean_checkout() {
    local changes
    changes="$(git_app status --porcelain --untracked-files=normal)" || return 1
    [[ -z $changes ]]
}
[[ $(git_app rev-parse HEAD) == "$BASE" ]] || fail 'Unexpected deployed commit; stop and re-plan this rollout.'
if [[ -f /etc/repbase-release ]]; then
    [[ $(cat /etc/repbase-release) == "$BASE" ]] || fail 'Release stamp differs from the expected live base; inspect deployment state first.'
fi
clean_checkout || fail 'Backend checkout is dirty; preserve and review those changes first.'
previous_branch="$(git_app symbolic-ref --quiet --short HEAD || true)"
git_app fetch --quiet --no-tags origin "refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
[[ $(git_app rev-parse "refs/remotes/origin/$BRANCH^{commit}") == "$TARGET" ]] || fail 'Branch head differs from the validated SHA.'
git_app merge-base --is-ancestor "$BASE" "$TARGET" || fail 'Candidate does not descend from the deployed base.'
type_changes="$(git_app diff --name-only --diff-filter=DT "$BASE" "$TARGET")"
[[ -z $type_changes ]] || fail 'Candidate deletes files or changes file types.'
changed_paths="$(git_app diff --name-only "$BASE" "$TARGET")"
changed_model=0
changed_migration=0
changed_view=0
while IFS= read -r path; do
    case "$path" in
        core/models.py) changed_model=1 ;;
        core/views.py) changed_view=1 ;;
        core/migrations/0081_plannerentry_source_schedule.py) changed_migration=1 ;;
        core/schedule_sync.py|core/test_schedule_planner_sync.py|TESTING.md) ;;
        deploy/deploy-calendar-sync-hotfix.sh|deploy/CALENDAR_SYNC_HOTFIX.md) ;;
        deploy/calendar_sync_release.py|core/test_calendar_sync_release.py|core/test_calendar_sync_shell.py|core/test_calendar_sync_systemd.py) ;;
        .github/workflows/ci.yml) ;; # Matching PostgreSQL client on CI only.
        *) fail "Unexpected changed path: $path" ;;
    esac
done <<< "$changed_paths"
[[ $changed_model -eq 1 && $changed_migration -eq 1 && $changed_view -eq 1 ]] || fail 'Expected calendar sync changes are incomplete.'
cmp -s "$0" <(git_app show "$TARGET:deploy/deploy-calendar-sync-hotfix.sh") || fail 'Helper does not match the validated commit.'
[[ $(git_app rev-parse HEAD) == "$BASE" ]] && clean_checkout || fail 'Checkout changed during preflight.'

# These are all known model-loading services on this beta host. Fail on a
# newly installed app unit so it cannot keep writing during schema changes.
# Certificate renewal does not load application models and stays untouched.
services=(repbase.service repbase-feed-stats.service repbase-media-screening.service repbase-ranking-model.service repbase-video.service)
timers=(repbase-feed-stats.timer repbase-media-screening.timer repbase-ranking-model.timer repbase-video.timer)
unit_listing="$(systemctl list-unit-files --no-legend --no-pager 'repbase*.service' 'repbase*.timer')"
loaded_units="$(systemctl list-units --all --plain --no-legend --no-pager 'repbase*.service' 'repbase*.timer')"
declare -A optional_units=()
while read -r unit _; do
    [[ -n $unit ]] || continue
    case "$unit" in
        repbase.service|repbase-feed-stats.service|repbase-media-screening.service|repbase-ranking-model.service|repbase-video.service) ;;
        repbase-feed-stats.timer|repbase-media-screening.timer|repbase-ranking-model.timer|repbase-video.timer) ;;
        repbase-push.service|repbase-push.timer) optional_units["$unit"]=1 ;;
        repbase-certbot.service|repbase-certbot.timer) ;;
        *) fail "Unreviewed backend unit: $unit. Review its quiescence requirements first." ;;
    esac
done <<< "$unit_listing
$loaded_units"
if [[ ${optional_units[repbase-push.service]:-0} == 1 ]]; then services+=(repbase-push.service); fi
if [[ ${optional_units[repbase-push.timer]:-0} == 1 ]]; then timers+=(repbase-push.timer); fi

declare -A originally_running=()
for unit in "${services[@]}" "${timers[@]}"; do
    [[ $(systemctl show --property=LoadState --value "$unit") == loaded ]] || fail "Expected unit is not loaded: $unit"
    state="$(systemctl show --property=ActiveState --value "$unit")"
    case "$state" in
        active|activating) originally_running["$unit"]=1 ;;
        inactive) originally_running["$unit"]=0 ;;
        *) fail "Unit requires attention before deployment: $unit ($state)" ;;
    esac
done
[[ ${originally_running[repbase.service]} == 1 ]] || fail 'Backend was not running; resolve its state before this rollout.'

record="$(mktemp -d /var/lib/repbase-calendar-sync.XXXXXXXX)"
chmod 700 "$record"
printf 'base=%s\ntarget=%s\nstarted=%s\n' "$BASE" "$TARGET" "$(date -Is)" > "$record/release"
for unit in "${services[@]}" "${timers[@]}"; do
    printf '%s %s\n' "$unit" "${originally_running[$unit]}" >> "$record/original-units"
done
if [[ -f /etc/repbase-release ]]; then
    cp /etc/repbase-release "$record/previous-release"
fi

run_app() (
    set -a
    # shellcheck disable=SC1091
    . /etc/repbase.env
    set +a
    cd "$APP"
    exec sudo -H -E -u "$APP_USER" "$@"
)
support() { run_app "$APP/.venv/bin/python" -m deploy.calendar_sync_release "$@"; }
# BEGIN QUIESCENCE CHECK
unit_is_quiescent() {
    local unit="$1" state job main_pid control_pid group root directory events key value extra populated=0
    [[ $(systemctl show --property=LoadState --value "$unit") == loaded ]] || return 1
    state="$(systemctl show --property=ActiveState --value "$unit")" || return 1
    job="$(systemctl show --property=Job --value "$unit")" || return 1
    [[ -z $job ]] || return 1
    if [[ $unit == *.timer ]]; then
        [[ $state == inactive ]]
        return $?
    fi
    [[ $unit == *.service ]] || return 1
    case "$state" in inactive|failed) ;; *) return 1 ;; esac
    main_pid="$(systemctl show --property=MainPID --value "$unit")" || return 1
    control_pid="$(systemctl show --property=ControlPID --value "$unit")" || return 1
    [[ $main_pid == 0 && $control_pid == 0 ]] || return 1
    group="$(systemctl show --property=ControlGroup --value "$unit")" || return 1
    [[ -n $group ]] || return 0
    # MainPID alone can miss remaining children. cgroup v2's populated flag
    # covers the whole subtree. Unreadable or malformed evidence fails closed.
    [[ $group == /* && $group != / && $group != //* ]] || return 1
    case "/${group#/}/" in */../*|*/./*) return 1 ;; esac
    [[ -r $CGROUP_ROOT/cgroup.controllers ]] || return 1
    root="$(realpath -e -- "$CGROUP_ROOT")" || return 1
    directory="$(realpath -m -- "$CGROUP_ROOT$group")" || return 1
    [[ $directory == "$root/"* ]] || return 1
    # The kernel can already have removed an empty, stopped unit's cgroup.
    [[ -e $directory ]] || return 0
    [[ -d $directory && -r $directory/cgroup.events ]] || return 1
    events="$(cat -- "$directory/cgroup.events")" || return 1
    while read -r key value extra; do
        if [[ $key == populated ]]; then
            [[ $value == 0 && -z $extra ]] || return 1
            populated=$((populated + 1))
        fi
    done <<< "$events"
    [[ $populated -eq 1 ]]
}
# END QUIESCENCE CHECK
record_unit_states() {
    local unit
    for unit in "${timers[@]}" "${services[@]}"; do
        printf '\nunit=%s\n' "$unit"
        systemctl show --property=LoadState,ActiveState,SubState,Result,MainPID,ControlPID,ControlGroup,Job "$unit" || return 1
    done >> "$record/unit-stop-evidence"
}
quiesce() {
    local unit failed=0
    # Stop all timers before their services, including initially idle timers.
    systemctl stop "${timers[@]}" || failed=1
    systemctl stop "${services[@]}" || failed=1
    record_unit_states || failed=1
    for unit in "${timers[@]}" "${services[@]}"; do
        if ! unit_is_quiescent "$unit"; then
            echo "Could not prove stopped: $unit (see unit-stop-evidence)." >&2
            failed=1
        fi
    done
    return "$failed"
}
reset_quiescent_worker_failure() {
    local unit="$1" state
    state="$(systemctl show --property=ActiveState --value "$unit")" || return 1
    if [[ $state == failed ]]; then
        # Evidence is already saved. Clear only a proven stopped worker's
        # retained failure; an active timer may not launch it immediately.
        unit_is_quiescent "$unit" || return 1
        systemctl reset-failed "$unit" || return 1
    fi
}
restore_unchanged_base() {
    local unit timer failed=0
    local -A unproven_workers=()
    # No candidate checkout/migration has even started. A worker-stop failure
    # must not strand the known unchanged web backend behind another stop gate.
    [[ $switch_started -eq 0 && $(git_app rev-parse HEAD) == "$BASE" ]] || return 1
    clean_checkout || return 1
    if [[ -f /etc/repbase-release ]]; then
        [[ $(cat /etc/repbase-release) == "$BASE" ]] || return 1
    fi
    if systemctl start repbase.service && wait_for_health; then
        echo 'Unchanged original backend restored; API health checks passed.' >&2
    else
        echo 'RECOVERY NEEDS ATTENTION: unchanged backend could not be started.' >&2
        failed=1
    fi
    # Independently attempt every originally active unit. One problematic
    # worker must not prevent restoration of the others or of their timers.
    for unit in "${services[@]:1}"; do
        timer="${unit%.service}.timer"
        if [[ ${originally_running[$unit]} == 1 || ${originally_running[$timer]:-0} == 1 ]]; then
            if ! reset_quiescent_worker_failure "$unit"; then
                failed=1
                unproven_workers["$unit"]=1
                continue
            fi
        fi
        if [[ ${originally_running[$unit]} == 1 ]]; then
            systemctl start --no-block "$unit" || failed=1
        fi
    done
    for unit in "${timers[@]}"; do
        if [[ ${originally_running[$unit]} == 1 ]]; then
            [[ ${unproven_workers[${unit%.timer}.service]:-0} == 0 ]] || continue
            systemctl start "$unit" || failed=1
        fi
    done
    verify_resumed_units || failed=1
    [[ $failed -eq 0 ]] || echo 'Some original units need attention; web recovery was attempted independently.' >&2
    return "$failed"
}
resume_original_units() {
    local unit timer
    for unit in "${services[@]:1}"; do
        timer="${unit%.service}.timer"
        if [[ ${originally_running[$unit]} == 1 || ${originally_running[$timer]:-0} == 1 ]]; then
            reset_quiescent_worker_failure "$unit" || return 1
        fi
    done
    systemctl start repbase.service || return 1
    for unit in "${services[@]:1}"; do
        if [[ ${originally_running[$unit]} == 1 ]]; then
            # Previously running one-shot workers may take time to drain.
            systemctl start --no-block "$unit" || return 1
        fi
    done
    for unit in "${timers[@]}"; do
        if [[ ${originally_running[$unit]} == 1 ]]; then
            systemctl start "$unit" || return 1
            systemctl is-active --quiet "$unit" || return 1
        fi
    done
    verify_resumed_units
}
verify_resumed_units() {
    local unit timer state substate result
    for unit in "${services[@]}"; do
        timer="${unit%.service}.timer"
        if [[ ${originally_running[$unit]} == 1 || ${originally_running[$timer]:-0} == 1 ]]; then
            [[ $(systemctl show --property=LoadState --value "$unit") == loaded ]] || return 1
            state="$(systemctl show --property=ActiveState --value "$unit")" || return 1
            substate="$(systemctl show --property=SubState --value "$unit")" || return 1
            result="$(systemctl show --property=Result --value "$unit")" || return 1
            # A successful one-shot can already be inactive; a queued/running
            # worker can still be activating. Do not accept a failed restart.
            case "$state" in active|activating|inactive) ;; *) return 1 ;; esac
            [[ $substate != auto-restart ]] || return 1
            [[ $result == success ]] || return 1
        fi
    done
}
smoke() {
    local health api
    systemctl is-active --quiet repbase.service || return 1
    health="$(curl --silent --show-error --connect-timeout 3 --max-time 10 -o /dev/null -w '%{http_code}' "$ORIGIN/health/")" || return 1
    api="$(curl --silent --show-error --connect-timeout 3 --max-time 10 -o /dev/null -w '%{http_code}' "$ORIGIN/api/v1/")" || return 1
    [[ $health == 200 && $api == 401 ]]
}
wait_for_health() {
    local attempt
    for attempt in 1 2 3 4 5; do
        if smoke; then return 0; fi
        sleep 2
    done
    return 1
}

quiescence_started=0
switch_started=0
release_changed=0
completed=0
restore_on_failure() {
    local code=$? current_ref restore_ref="$BASE" safe=0 restored=0
    trap - EXIT HUP INT TERM
    set +e
    if [[ $completed -eq 1 ]]; then return "$code"; fi
    printf 'failed=%s\nexit_code=%s\n' "$(date -Is)" "$code" >> "$record/release"
    if [[ $quiescence_started -eq 0 ]]; then return "$code"; fi
    echo "Rollout failed. Release/backup record: $record" >&2
    if [[ $switch_started -eq 0 ]]; then
        if restore_unchanged_base; then
            printf 'restored_unchanged_base=%s\n' "$BASE" >> "$record/release"
        else
            echo 'RECOVERY NEEDS ATTENTION: inspect original service states; no checkout/schema rollback attempted.' >&2
        fi
        return "$code"
    fi
    # Stop any services restarted before a failed health/release-stamp check.
    if ! quiesce; then
        echo 'RECOVERY NEEDS ATTENTION: not all application units stopped. No checkout/schema rollback attempted.' >&2
        return "$code"
    fi
    current_ref="$(git_app rev-parse HEAD)"
    if [[ $switch_started -eq 0 || $current_ref == "$BASE" ]]; then
        # No candidate ever served or migrated; only restore original units.
        if clean_checkout; then safe=1; fi
    elif [[ $current_ref == "$TARGET" ]] && clean_checkout && support rollback-safe; then
        safe=1
    fi
    if [[ $safe -ne 1 ]]; then
        echo 'RECOVERY NEEDS ATTENTION: old-code compatibility could not be proved.' >&2
        echo 'Application services/timers remain stopped. Keep the candidate checkout; prefer a reviewed forward fix.' >&2
        echo 'Do not reverse migration 0081, detach provenance, or restore the database without a separate recovery review.' >&2
        return "$code"
    fi
    if [[ $current_ref == "$BASE" ]]; then
        restored=1
    else
        if [[ -n $previous_branch && $(git_app rev-parse "$previous_branch") == "$BASE" ]]; then
            restore_ref="$previous_branch"
        fi
        if [[ $restore_ref == "$BASE" ]]; then
            git_app checkout --quiet --detach "$BASE" && restored=1
        else
            git_app checkout --quiet "$restore_ref" && restored=1
        fi
    fi
    if [[ $restored -ne 1 ]]; then
        echo 'RECOVERY NEEDS ATTENTION: original checkout could not be restored; application units remain stopped.' >&2
        return "$code"
    fi
    if [[ $release_changed -eq 1 ]]; then
        if [[ -f $record/previous-release ]]; then
            cp "$record/previous-release" /etc/repbase-release
        else
            rm -f -- /etc/repbase-release
        fi
    fi
    if resume_original_units && wait_for_health && verify_resumed_units; then
        echo 'Original backend restored; health checks passed. Any additive nullable schema is retained, not reversed.' >&2
        printf 'restored_base=%s\n' "$BASE" >> "$record/release"
    else
        echo 'RECOVERY NEEDS ATTENTION: original code restored, but service/health checks did not pass.' >&2
    fi
    return "$code"
}
trap restore_on_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Release record: $record"
echo 'Temporarily stopping backend and application workers for the backup and migration.'
quiescence_started=1
quiesce || fail 'Could not quiesce all application units; no checkout change is permitted.'
[[ $(git_app rev-parse HEAD) == "$BASE" ]] && clean_checkout || fail 'Checkout changed before migration.'
switch_started=1
git_app checkout --quiet --detach "$TARGET"
support preflight > "$record/before.json"
echo 'Creating and validating a private database backup; no user tasks are being deleted.'
support backup > "$record/database.dump"
[[ -s $record/database.dump ]] || fail 'Database backup is empty.'
pg_restore --list "$record/database.dump" > "$record/database-archive.list"
[[ -s $record/database-archive.list ]] || fail 'Database archive validation produced no entries.'
sha256sum "$record/database.dump" > "$record/database.dump.sha256"
echo 'Applying only core.0081_plannerentry_source_schedule with bounded database lock waits.'
support migrate
support verify < "$record/before.json" > "$record/after.json"
echo 'Checking existing backend configuration; no acknowledgement/bypass flags.'
run_app "$APP/.venv/bin/python" "$APP/manage.py" check --deploy
resume_original_units
wait_for_health || fail 'Required health checks did not pass (health=200, unauthenticated API=401).'
verify_resumed_units || fail 'An application worker failed while resuming; inspect the release record.'
printf 'validated=%s\nhealth=200\napi_unauthenticated=401\n' "$(date -Is)" >> "$record/release"
release_changed=1
printf '%s\n' "$TARGET" > /etc/repbase-release
completed=1
echo "Deployed $TARGET. Backend active; application timers restored to their original state."
echo '/health/ returned 200; unauthenticated /api/v1/ returned 401.'
echo 'Existing unlinked stale planner tasks are preserved and require separate, exact-item review.'
