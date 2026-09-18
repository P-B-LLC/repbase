# Deploying RepBase

The droplet runs one checkout of this repository at `/home/django/app`, served
by gunicorn under the `repbase` systemd unit and fronted by nginx.

## What is running right now

```bash
git -C /home/django/app rev-parse --short HEAD
```

That is the whole answer. The application directory is a git checkout, so the
running commit is a fact rather than something to reconstruct. `deploy.sh` also
writes it to `/etc/repbase-release` after a successful deploy.

Earlier the code was uploaded rather than cloned, and nothing on the box
recorded its origin. If you find yourself copying files up again, that is the
regression this directory exists to prevent.

## A note on the script updating itself

`deploy.sh` lives in the tree it checks out, so it copies itself to a
temporary file and runs from there. bash reads a script incrementally, and
replacing the file mid-run makes it resume at a byte offset into different
text — which is what happened the first time this ran.

The version you invoke is the version that runs to completion. The one it
checks out takes effect on the next deploy.

## Deploying

```bash
ssh root@<droplet>
/home/django/app/deploy/deploy.sh          # origin/main
/home/django/app/deploy/deploy.sh v1.2.0   # a tag, branch or commit
```

The script fetches, checks out, installs both requirement files, migrates,
collects static, runs `check --deploy`, restarts and then probes `/health/`.
It refuses to run if the checkout has uncommitted changes, because that means
somebody edited the server by hand and overwriting it would destroy the only
copy.

## Deploys are gated on configuration

`check --deploy` runs before the restart, and the repository adds its own
checks on top of Django's. A failed deploy puts the checkout back on the
commit that is still serving, so `HEAD` keeps telling the truth.

Mail is configured now, so `core.E002` and `core.E003` are answered. What
remains is:

- `core.E006` — social publishing is on without automated moderation

## Deferring a check, out loud

A check that is known, accepted and deliberately deferred can be named in
`ACKNOWLEDGE_CHECKS`:

```bash
ACKNOWLEDGE_CHECKS="core.E006" /home/django/app/deploy/deploy.sh
```

Anything *not* named still stops the deploy, so this never degrades into "skip
the checks" — a new fault in the commit being deployed fails exactly as it did
before. Each deploy that uses it prints a loud banner and appends a line to
`/var/log/repbase-deploy.log`.

Naming a check here is a decision to run without that protection. `core.E006`
says in its own hint not to bypass it to ship, and that remains true: listing
it defers the problem rather than answering it. Posts and comments currently
reach other people without being screened.

## Layout

| Path | What it is |
|---|---|
| `/home/django/app` | the checkout; `WorkingDirectory` of the unit |
| `/home/django/app/.venv` | the virtualenv |
| `/home/django/app/staticfiles` | collectstatic output, aliased by nginx at `/static/` |
| `/home/django/app/media` | uploads, proxied through Django |
| `/etc/repbase.env` | secrets, root-only, loaded by systemd |
| `/etc/repbase-release` | the commit the last deploy left running |
| `/var/www/rytivo-web` | the built React client |

## Configuration

`configure.py` writes `/etc/repbase.env` once, via a hidden prompt, and refuses
to overwrite it. Secrets are never echoed and never committed. `smoke.py` is a
post-deploy request check.

The service currently runs on `config.settings`. Moving it to
`config.production` needs Redis, the managed database's CA certificate for
`sslmode=verify-full`, an email provider, and the OpenAI key with its
disclosure flag.
