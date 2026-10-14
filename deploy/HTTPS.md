# HTTPS rollout — 14 October 2026

## Decision and scope

Keep nginx as the HTTP redirect/TLS edge for both the React build and API.
Enable Django's redirect as defense in depth, trusting only nginx's overwritten
X-Forwarded-Proto. Gunicorn is bound exclusively to 127.0.0.1:8001. HTTPS requests
do not incur an extra redirect or database work. Preserve caching, video byte
ranges, cookies, API origins, certificates and existing HTTP ACME challenges.
No iOS or React application-code change is needed.

Start HSTS at **3600 seconds**, without includeSubDomains or preload. The edge
emits the header on successful/error/static responses and hides Django's copy to
avoid duplicate headers. Locations with add_header must explicitly repeat HSTS
on nginx 1.24. Do not increase the duration until TLS renewal and device/browser
flows are verified. Rollback cannot erase an HSTS policy already cached by clients;
the initial one-hour scope limits this risk.

## Files and deployment

- `config/settings.py`: separate subdomain opt-in from max-age; default false.
- `deploy/configure_https.py`: default read-only root preflight, explicit --apply,
  private /root backup, atomic writes, nginx test, service restart/reload and probes.
- `core/test_https_hardening.py`: middleware, duplicate header prevention,
  inheritance, unsupported configuration rejection and environment preservation.
- Web repository: keep `deploy/rytivo.app.nginx.conf` aligned with this header
  policy. Do NOT copy that whole template over the live file: the inspected live
  site has video includes and other differences. The rollout edits only headers.

After deploying the tested backend commit through the normal deployment flow:

```bash
python3 /home/django/app/deploy/configure_https.py
python3 /home/django/app/deploy/configure_https.py --apply
```

Root is required. Do not print /etc/repbase.env or copy secrets to a terminal.
The script preserves unrelated environment keys, including moderation settings.
It intentionally refuses unexpected nginx layouts rather than replacing them.
It does not switch to config.production, change subdomains, or disable checks.
Record the backup path printed by the script. Contents are numbered: 0 = nginx
site, 1 = video snippet, 2 = environment. Treat the entire backup as secret.

The checked-in video snippet also carries the HSTS directive so future video
configuration does not silently drop it. It is included only in the TLS vhost.

## Verification and outstanding items

Check HTTPS `/`, a real `/assets/` file, `/api/v1/` (401 without credentials),
`/access/reports/` (302 to sign-in), and a private-video denial. Each should have
exactly one `Strict-Transport-Security: max-age=3600` header. Verify HTTP redirects
to HTTPS, www redirects to the canonical hostname, sign-in/CSRF and iOS API/media
work, and a valid signed video supports playback/seeking. Never log signed URLs.
Run Django deployment checks in the service environment: W004/W008 should be gone.
Moderation E006/core.W008 are unrelated and remain unresolved for limited beta.

The old default HTTP vhost and loopback-only legacy tunnel were not removed.
The tunnel's HTTP requests will now redirect; use public HTTPS for normal access.
Inventory unused vhosts separately before removing anything.

References: https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header
and https://nginx.org/en/docs/http/ngx_http_headers_module.html#add_header.
