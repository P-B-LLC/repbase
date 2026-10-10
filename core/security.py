"""Response headers, and what an error report may contain.

Django's SecurityMiddleware and XFrameOptionsMiddleware already send
`X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
`Cross-Origin-Opener-Policy: same-origin` and `X-Frame-Options: DENY`, and
production adds HSTS. What was missing is below.

## Two policies, chosen by what the response is

A Content-Security-Policy suited to the admin would be loose for a JSON
response, and one suited to JSON would break the admin. Django's own CSP
middleware applies one policy to everything, so this chooses per response:

- JSON and everything else that is not a page gets `default-src 'none'`, the
  policy OWASP gives for APIs. Nothing in a JSON body should ever run, load or
  be framed, and this says so to any browser that is somehow made to render
  one.
- HTML pages -- the admin, the API docs, DRF's browsable API, the staff
  member list -- may load scripts only from this origin and from the CDN the
  docs page pins. Inline *styles* are allowed because the docs page and the
  browsable API both use them; inline *scripts* are not, which is the half of
  the policy that stops injected markup from running.
- Uploaded media gets no policy at all. Opening a photo in its own tab makes
  the browser build a document around it, and `default-src 'none'` would
  block the very image that document exists to show. The files are served
  with their real type and `nosniff`, and nothing accepted as an upload can be
  interpreted as a page.
"""

import re

from django.conf import settings
from django.http import JsonResponse
from django.utils.csp import CSP, build_policy
from django.views.debug import SafeExceptionReporterFilter

#: The CDN the API docs load Swagger UI from. Read from the setting that also
#: builds SPECTACULAR_SETTINGS, so the policy and the page cannot disagree.
DOCS_CDN = getattr(settings, "API_DOCS_CDN", "https://cdn.jsdelivr.net")

API_POLICY = {
    "default-src": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "form-action": [CSP.NONE],
}

PAGE_POLICY = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, DOCS_CDN],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE, DOCS_CDN],
    "img-src": [CSP.SELF, "data:", DOCS_CDN],
    "font-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "form-action": [CSP.SELF],
}

#: Device features nothing served from here has any use for. A page that
#: cannot ask for the camera cannot be tricked into asking for it.
PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer", "camera", "geolocation", "gyroscope", "magnetometer",
        "microphone", "payment", "usb",
    )
)

_MEDIA_TYPES = ("image/", "video/", "audio/")


class SecurityHeadersMiddleware:
    """Content-Security-Policy, Permissions-Policy, CORP and API caching.

    Every header is set only when the response has not set it already, so a
    view with a reason to differ can say so and be listened to.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        content_type = response.get("Content-Type", "")

        if not content_type.startswith(_MEDIA_TYPES) and "Content-Security-Policy" not in response:
            policy = PAGE_POLICY if content_type.startswith("text/html") else API_POLICY
            response["Content-Security-Policy"] = build_policy(policy)

        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        # Another site may not embed an API answer or a page. Photos and clips
        # are the exception, and must say so explicitly: their URLs are
        # absolute and built from the host the API was asked on, so the page
        # showing them is not always their origin -- in development the web
        # app is on Vite's port and the media on Django's, and a CDN would do
        # the same in production. With `same-origin` here Chrome refused every
        # clip and photo in the running web app (ERR_BLOCKED_BY_RESPONSE),
        # which no test of the API alone could see. Access to a file is
        # decided by its signature, not by who embeds it.
        response.setdefault(
            "Cross-Origin-Resource-Policy",
            "cross-origin" if content_type.startswith(_MEDIA_TYPES) else "same-origin",
        )

        # API answers are about one signed-in person. Nothing set a caching
        # header on them, which leaves a shared proxy or a browser's disk free
        # to keep somebody's profile or feed. Photos set their own lifetime
        # and are untouched.
        if request.path.startswith("/api/") and "Cache-Control" not in response:
            response["Cache-Control"] = "no-store"
        return response


class RefuseNullBytesMiddleware:
    """Answer 400 to a query string carrying a NUL byte.

    PostgreSQL cannot store or compare text containing NUL, and raises when a
    query parameter carrying one reaches a filter -- so `?search=%00` was a
    500 on every search the API has, in production and never in development,
    because SQLite does not mind. Request bodies were already safe: DRF's
    CharField refuses NUL. Query strings go straight to filters, and there are
    a dozen of them, so the refusal is here once rather than in each.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if "\x00" in request.path or any(
            "\x00" in key or any("\x00" in value for value in values)
            for key, values in request.GET.lists()
        ):
            return JsonResponse(
                {"detail": "Query parameters cannot contain NUL characters."}, status=400
            )
        return self.get_response(request)


class RedactingExceptionReporterFilter(SafeExceptionReporterFilter):
    """Django's error-report filter, made to hide the Authorization header.

    The stock pattern hides settings and request headers whose names contain
    API, TOKEN, KEY, SECRET, PASS, SIGNATURE or HTTP_COOKIE.
    `HTTP_AUTHORIZATION` contains none of those, so a report of an exception
    raised during an authenticated request -- the technical 500 page in
    development, an admin email in production -- printed the caller's token
    in full. `AUTH` closes that, and costs nothing: the only setting it newly
    hides is the list of password validators.
    """

    hidden_settings = re.compile(
        "API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE", flags=re.I
    )
