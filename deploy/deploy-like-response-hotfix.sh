#!/usr/bin/env bash
# Root-only, code-only hotfix. No dependencies, migrations, or environment edits.
# Usage: bash deploy/deploy-like-response-hotfix.sh <validated-full-commit-sha>
set +x
set -euo pipefail

APP=/home/django/app
APP_USER=django
BASE=de27d1b000de2f9e1c9b76154bfde3433840b3f8
BRANCH=fix/beta-like-response-2026-10-28
ORIGIN=https://rytivo.app
TARGET="${1:-}"

fail() { echo "$*" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || fail 'Run this helper as root.'
[[ $# -eq 1 && $TARGET =~ ^[0-9a-f]{40}$ ]] || fail 'Supply exactly one validated, full commit SHA.'

# Checkout replaces this file. Execute an immutable temporary copy instead.
if [[ ${REPBASE_LIKE_HOTFIX_PINNED:-} != 1 ]]; then
    pinned="$(mktemp /tmp/repbase-like-hotfix.XXXXXXXX)"
    trap 'rm -f -- "$pinned"' EXIT
    cp -- "$0" "$pinned"
    chmod 700 "$pinned"
    REPBASE_LIKE_HOTFIX_PINNED=1 bash "$pinned" "$TARGET"
    exit $?
fi

[[ -d $APP/.git && -x $APP/.venv/bin/python ]] || fail 'Expected backend checkout/virtualenv is missing.'
[[ -r /etc/repbase.env ]] || fail 'Existing backend environment file is missing.'
exec 9>/run/lock/repbase-deploy.lock
flock -n 9 || fail 'Another deployment holds the deployment lock.'
git_app() { sudo -H -u "$APP_USER" git -C "$APP" "$@"; }
clean_checkout() {
    local changes
    changes="$(git_app status --porcelain --untracked-files=normal)" || return 1
    [[ -z $changes ]]
}
[[ $(git_app rev-parse HEAD) == "$BASE" ]] || fail 'Unexpected deployed commit; stop and re-plan this hotfix.'
clean_checkout || fail 'Backend checkout is dirty; preserve and review those changes first.'
previous_branch="$(git_app symbolic-ref --quiet --short HEAD || true)"

git_app fetch --quiet --no-tags origin "refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
[[ $(git_app rev-parse "refs/remotes/origin/$BRANCH^{commit}") == "$TARGET" ]] || fail 'Branch head differs from the validated SHA.'
git_app merge-base --is-ancestor "$BASE" "$TARGET" || fail 'Hotfix does not descend from the deployed base.'
type_changes="$(git_app diff --name-only --diff-filter=DT "$BASE" "$TARGET")"
[[ -z $type_changes ]] || fail 'Hotfix deletes files or changes file types.'
changed_paths="$(git_app diff --name-only "$BASE" "$TARGET")"
changed_view=0
while IFS= read -r path; do
    case "$path" in
        core/views.py) changed_view=1 ;;
        core/test_post_likes.py|deploy/deploy-like-response-hotfix.sh|deploy/LIKE_RESPONSE_HOTFIX.md) ;;
        *) fail "Unexpected changed path: $path" ;;
    esac
done <<< "$changed_paths"
[[ $changed_view -eq 1 ]] || fail 'Expected response-readback fix is absent.'
cmp -s "$0" <(git_app show "$TARGET:deploy/deploy-like-response-hotfix.sh") || fail 'Helper does not match the validated commit.'
[[ $(git_app rev-parse HEAD) == "$BASE" ]] && clean_checkout || fail 'Checkout changed during preflight.'

record="$(mktemp -d /var/lib/repbase-like-hotfix.XXXXXXXX)"
printf 'base=%s\ntarget=%s\nstarted=%s\n' "$BASE" "$TARGET" "$(date -Is)" > "$record/release"
if [[ -f /etc/repbase-release ]]; then
    cp -p /etc/repbase-release "$record/previous-release"
fi
switched=0
release_changed=0
completed=0

# Read only the existing service environment. Never print or copy its secrets.
run_app() (
    set -a
    # shellcheck disable=SC1091
    . /etc/repbase.env
    set +a
    cd "$APP"
    exec sudo -H -E -u "$APP_USER" "$@"
)

smoke() {
    local health api
    systemctl is-active --quiet repbase || return 1
    health="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        -o /dev/null -w '%{http_code}' "$ORIGIN/health/")" || return 1
    api="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        -o /dev/null -w '%{http_code}' "$ORIGIN/api/v1/")" || return 1
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

restore_on_failure() {
    local code=$? restore_ref="$BASE" checkout_restored=0
    trap - EXIT HUP INT TERM
    set +e
    if [[ $completed -eq 0 && $switched -eq 1 ]]; then
        echo "Hotfix failed; restoring $BASE and restarting the previous backend." >&2
        if [[ -n $previous_branch && $(git_app rev-parse "$previous_branch") == "$BASE" ]]; then
            restore_ref="$previous_branch"
        fi
        if [[ $restore_ref == "$BASE" ]]; then
            git_app checkout --quiet --detach "$BASE"
        else
            git_app checkout --quiet "$restore_ref"
        fi
        if [[ $? -eq 0 ]]; then checkout_restored=1; fi
        if [[ $checkout_restored -eq 1 ]] && systemctl restart repbase && wait_for_health; then
            echo 'Previous backend restored; health checks passed.' >&2
        else
            echo 'ROLLBACK NEEDS ATTENTION: inspect the checkout and repbase service.' >&2
        fi
        if [[ $release_changed -eq 1 && $checkout_restored -eq 1 ]]; then
            if [[ -f $record/previous-release ]]; then
                cp -p "$record/previous-release" /etc/repbase-release
            else
                rm -f -- /etc/repbase-release
            fi
        fi
        printf 'failed=%s\n' "$(date -Is)" >> "$record/release"
    fi
    return "$code"
}
trap restore_on_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Release record: $record"
switched=1
git_app checkout --quiet --detach "$TARGET"
echo 'Checking existing backend configuration (no acknowledgement/bypass flags).'
run_app "$APP/.venv/bin/python" "$APP/manage.py" check --deploy
systemctl restart repbase
wait_for_health || fail 'Required health checks did not pass (health=200, unauthenticated API=401).'
printf 'validated=%s\nhealth=200\napi_unauthenticated=401\n' "$(date -Is)" >> "$record/release"
release_changed=1
printf '%s\n' "$TARGET" > /etc/repbase-release
completed=1
echo "Deployed $TARGET. Backend active; /health/ returned 200 and /api/v1/ returned 401."
