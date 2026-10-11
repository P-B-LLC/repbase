"""Rate limits that DRF's stock throttles cannot express on their own.

Two shapes are missing from what DRF ships.

The first is a limit on *failed* sign-ins per username. `login` already caps
attempts per address, which does nothing against a password-guessing run that
is spread across many addresses at one account -- the usual shape of
credential stuffing. Counting every attempt per username would hand anybody a
way to lock a stranger out by typing their name wrong twenty times, so only
failures are counted, and a password reset clears the count.

The second is choosing a scope by what a request does rather than by which
viewset answers it. A viewset carries one `throttle_scope`, so PostViewSet's
"post" was being spent by reads and likes as well as by publishing.
"""

import functools
import hashlib

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import Throttled
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle


class ThrottleByActionMixin:
    # Picks the ScopedRateThrottle scope from the viewset action. Comments
    # rather than a docstring: drf-spectacular documents a view with the first
    # docstring in its class hierarchy, and a viewset with none of its own
    # would be described to API readers as a throttling mixin.
    #
    # An action missing from the map has no scoped limit, which is the same
    # answer ScopedRateThrottle gives a view with no `throttle_scope`: only
    # the per-user and per-address ceilings that apply to everything still
    # apply.

    action_throttle_scopes = {}

    @property
    def throttle_scope(self):
        return self.action_throttle_scopes.get(getattr(self, "action", None))


class IdentityLimit(SimpleRateThrottle):
    """A count kept against one account identity -- a username, an email --
    wherever the requests come from.

    Keyed on a hash of the casefolded identity rather than the identity
    itself: the cache is not where account names should be readable, and a
    hash is a key of fixed length and alphabet whatever somebody typed.

    The same count is kept whether or not such an account exists. A limit that
    only engaged for real accounts would answer the question it is meant to
    leave unanswered.
    """

    scope = None

    def __init__(self, identity):
        super().__init__()
        self.identity = (identity or "").strip().casefold()

    def get_rate(self):
        # Read live rather than from the class attribute SimpleRateThrottle
        # copies at import, so a changed setting is the setting in force.
        return api_settings.DEFAULT_THROTTLE_RATES.get(self.scope)

    def get_cache_key(self, request=None, view=None):
        digest = hashlib.sha256(self.identity.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}

    def _load(self):
        self.key = self.get_cache_key()
        self.now = self.timer()
        history = self.cache.get(self.key, [])
        self.history = [stamp for stamp in history if stamp > self.now - self.duration]

    def exceeded(self):
        """Whether this identity has used up its allowance lately."""
        if not self.identity or self.rate is None:
            return False
        self._load()
        return len(self.history) >= self.num_requests

    def check(self):
        """Raise 429 if this identity has used up its allowance lately."""
        if self.exceeded():
            raise Throttled(wait=self.wait())

    def record(self):
        if not self.identity or self.rate is None:
            return
        self._load()
        self.history.insert(0, self.now)
        self.cache.set(self.key, self.history, self.duration)

    def clear(self):
        """Forget the count, after the owner has proved who they are."""
        if self.identity:
            self.cache.delete(self.get_cache_key())


class FailedSignInLimit(IdentityLimit):
    """Failed sign-ins against one username, wherever they come from.

    Counted by `core.auth_backends.SignInLimitBackend`, so every way in --
    the API, the admin, the browsable API, the private workspaces -- spends
    the same allowance. Checked again there before any password is compared.
    """

    scope = "login_username"

    record_failure = IdentityLimit.record


class PasswordResetIssueLimit(IdentityLimit):
    """Reset codes issued for one email address, from any source.

    The per-address limit could be walked around with more addresses, and
    each new code bought five fresh guesses -- and another email in the
    owner's inbox.
    """

    scope = "password_reset_account"


class PasswordResetFailureLimit(IdentityLimit):
    """Wrong reset codes submitted for one email address, across every code.

    `PasswordResetCode.MAX_ATTEMPTS` bounds the guesses at one code; this
    bounds them across all the codes an attacker can have issued.
    """

    scope = "password_reset_account_failures"


def client_address(request):
    """The client's address, as the nearest trusted proxy recorded it.

    The same reading DRF's throttles make with NUM_PROXIES set: the entry
    that many hops from the end of X-Forwarded-For, which is the one the
    proxy wrote rather than one the client prepended.

    With NUM_PROXIES unset the header is not believed at all -- there is no
    proxy vouching for it, so it is whatever the client chose -- and the
    socket address answers instead. DRF uses the whole header in that case,
    which would let a client pick a fresh identity per request here.
    """
    remote = request.META.get("REMOTE_ADDR", "") or ""
    proxies = api_settings.NUM_PROXIES
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if not proxies or not forwarded:
        return remote
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if not hops:
        return remote
    return hops[-min(proxies, len(hops))]


#: How long the per-address count of Django sign-in attempts lasts.
SIGN_IN_WINDOW_SECONDS = 15 * 60


def sign_in_attempt_refusal(request):
    """None if this sign-in may go ahead, else the response refusing it.

    For Django's own sign-in pages -- the admin, the browsable API, insights
    and the access workspace -- which DRF's throttles never see. One
    allowance across all of them, per client address. Never keyed on the raw
    address, which the cache has no business holding.
    """
    allowed = getattr(settings, "DJANGO_LOGIN_ATTEMPTS_PER_ADDRESS", 5)
    key = "django-login:" + salted_hmac("django-login", client_address(request)).hexdigest()
    try:
        cache.add(key, 0, SIGN_IN_WINDOW_SECONDS)
        attempts = cache.incr(key)
    except Exception:
        # Do not fail open if the shared rate limiter is unavailable.
        return HttpResponse("Sign-in temporarily unavailable.", status=503)
    if attempts > allowed:
        return HttpResponse("Too many attempts. Try again in 15 minutes.", status=429)
    return None


def limit_sign_in_attempts(view):
    """Apply `sign_in_attempt_refusal` to the POSTs a sign-in view receives."""

    @functools.wraps(view)
    def limited(request, *args, **kwargs):
        if request.method == "POST":
            refusal = sign_in_attempt_refusal(request)
            if refusal is not None:
                return refusal
        return view(request, *args, **kwargs)

    return limited
