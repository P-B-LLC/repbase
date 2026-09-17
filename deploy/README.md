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
