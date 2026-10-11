# Limited local-video beta

This feature is disabled until explicitly configured. It does not store video
bytes in the database. The authenticated existing raw upload endpoint streams
at most 15 MiB to a spool, copies it to private disk and queues work. No encoder
is invoked in an API request. This is intentionally not direct-to-storage:
the API handles the bounded upload, while nginx handles production playback.

## Limits

- MP4/MOV/M4V, H.264 or HEVC input, 15 MiB, 30 seconds, at most 1920x1080
  pixels in either orientation, at most 60 input fps.
- One pending upload per account, five accepted uploads per UTC day. Removing
  a video does not reset the daily counter. Ten pending jobs globally.
- One worker process, one encoder thread, one 480-class MP4, 30 fps, AAC audio,
  fast-start metadata and a JPEG thumbnail. No autoplay or feed-wide downloads.
- 2 GiB tracked video storage budget; preserve at least 1 GiB free disk plus
  upload/output headroom. Existing photos, logs and other services also use disk.
- Three processing attempts, durable leases, retry delays and stale-worker
  fencing. Invalid clips fail permanently; failed/abandoned objects are cleaned.

## Configure and validate before enabling

Install distro-maintained ffmpeg and ffprobe. Create `/var/lib/rytivo/videos`
owned by django, group-readable by nginx, never world-writable. Do not place it
under public MEDIA_ROOT. Keep source and output directories in media backups;
database backups alone cannot restore video files.

Set in the server's private environment file:

```
VIDEO_PIPELINE_ENABLED=true
VIDEO_STORAGE_BACKEND=local
VIDEO_LOCAL_ROOT=/var/lib/rytivo/videos
VIDEO_LOCAL_ACCEL_PREFIX=/_private_video/
VIDEO_MODERATION_BYPASS=true
```

The bypass is an explicitly temporary beta exception. Processing, ownership,
visibility, blocking, report handling and takedown checks remain enforced.
Turn it off before opening unreviewed social uploads to strangers.

Add inside the nginx server handling API requests:

```
location /_private_video/ {
    internal;
    alias /var/lib/rytivo/videos/;
    add_header X-Content-Type-Options nosniff always;
    add_header Cache-Control "private, no-store" always;
}
```

Keep the upload request-body ceiling small (15m for the raw video-upload route)
and bound concurrent uploads/timeouts at nginx. Signed asset requests recheck
post visibility and approval before issuing X-Accel-Redirect. Test GET, HEAD,
Range, revoked access and direct internal-path rejection. No public directory
listing or static alias to the storage root may be installed.

For example, define `limit_conn_zone $server_name zone=video_uploads:1m;`
in nginx's http context, then use this route in the server context (keep your
existing proxy headers and upstream address if they differ):

```
location ~ ^/api/v1/social/posts/[0-9]+/video/$ {
    client_max_body_size 15m;
    client_body_timeout 30s;
    limit_conn video_uploads 2;
    limit_conn_status 429;
    proxy_request_buffering on;
    proxy_read_timeout 60s;
    include proxy_params;
    proxy_pass http://127.0.0.1:8001;
}
```

Keep nginx's temporary request-body directory on monitored disk. Run
`nginx -t` before reload. These are configuration examples, not already-applied
changes to the running server.

Apply migrations, check configuration, install repbase-video.service/timer,
run a single batch manually, and verify it before starting the timer. The
service is restricted to 25% of one CPU and 192 MiB memory, with low priority.
Do not increase these limits to mask failures on a small API droplet. Measure
API latency, free memory and encoder completion on the actual droplet first;
if clips hit the memory/time limits, reduce input limits or move the worker.
The filesystem worker lock also prevents overlapping manual invocations.

## Monitoring and recovery

`python manage.py process_videos --stats` reports job states, tracked bytes,
upload/playback failures, processing count/duration and moderation bypasses.
Use nginx access logs for actual video bytes delivered and bandwidth (including
206 responses); redirect/request counts are not bandwidth measurements.
Alert on old processing jobs, repeated failures, cleanup failures, high disk
usage, API latency and memory pressure. Daily aggregate metrics expire at 90 days.

Failed jobs are shown to their author with refresh/re-upload controls. A retry
uses the existing post rather than creating another. Worker crashes recover
after the lease expires. Stop the timer to stop processing; disabling the
pipeline stops new asynchronous uploads but does not revoke ready assets.

This is not a production load-test result. Activation requires a real-server
clip test and resource measurements. Object storage and a separate worker
remain the scaling path; the storage adapter already isolates that boundary.
Changing the storage backend setting does not migrate existing files: copy
and verify assets and plan reference migration before a future switch to S3.

The native implementation is on IOS-Frontend's `feat/video-beta` branch.
It imports file-backed clips without allocating the full video in memory,
checks limits before posting, uploads through the authenticated generated
client, retries on the existing post, and creates a player only after a tap.
Real-device visual and playback validation is still required.

## Deployed configuration (2026-10-11)

The backend feature branch preserves the live account-controls branch.
The droplet has distro FFmpeg, a setgid private media root owned by
`django:rytivo-media`, nginx group read access, bounded upload ingress,
and the constrained worker/timer. `configure_video.py` backs up the private
environment before changing only video settings. Native code is a separate
branch, not yet merged into the release branch.

Playback access logs use `$uri`, not `$request` or `$request_uri`, so signed
query capabilities are not logged. The same sanitized format applies after
the internal redirect. Media-route nginx error logs are suppressed because
nginx includes the original signed request in errors; use sanitized access
status/latency, the queue metrics and Django errors to diagnose failures.
Include `/var/log/nginx/rytivo-video-access.log` in log rotation.

Validated: real MP4/MOV/HEVC encoding under the worker limits, PostgreSQL
queue concurrency, ownership/visibility revocation regressions, server
health, rejected unsigned asset requests, and internal-path rejection.
Still required: a device upload through the deployed API, actual signed
playback/Range through nginx, and an off-droplet media backup/restore drill.
