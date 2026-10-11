"""The one place a password is checked, with the per-username limit on it.

The API's sign-in counted failures against a username, but Django's own
sign-in pages -- the admin, the browsable API at /api-auth/login/, insights
and the access workspace -- call `authenticate()` directly and counted
nothing. /api-auth/login/ signs any account in, and the session it sets works
on every API endpoint, so it was an unlimited way round the API's limit.

Every one of those pages, and the API, goes through `authenticate()`, and
`authenticate()` goes through here. So the limit lives here, once.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied

from .throttles import FailedSignInLimit


class SignInLimitBackend(ModelBackend):
    """Django's ModelBackend, refusing a username past its failure limit.

    Past the limit the right password is refused like a wrong one, before it
    is compared, so a guessing run learns nothing more until the window
    passes. PermissionDenied rather than None: `authenticate()` stops at it
    rather than asking another backend.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        limit = FailedSignInLimit(username if isinstance(username, str) else "")
        if limit.exceeded():
            raise PermissionDenied
        user = super().authenticate(request, username=username, password=password, **kwargs)
        if user is None and password is not None:
            limit.record_failure()
        return user
