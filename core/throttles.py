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

import hashlib

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


class FailedSignInLimit(SimpleRateThrottle):
    """Failed sign-ins against one username, wherever they come from.

    Keyed on a hash of the casefolded username rather than the name itself:
    the cache is not where account names should be readable, and a hash is a
    key of fixed length and alphabet whatever somebody typed.

    The same count is kept whether or not such an account exists. A limit that
    only engaged for real usernames would answer the question it is meant to
    leave unanswered.
    """

    scope = "login_username"

    def __init__(self, username):
        super().__init__()
        self.username = (username or "").strip().casefold()

    def get_rate(self):
        # Read live rather than from the class attribute SimpleRateThrottle
        # copies at import, so a changed setting is the setting in force.
        return api_settings.DEFAULT_THROTTLE_RATES.get(self.scope)

    def get_cache_key(self, request=None, view=None):
        digest = hashlib.sha256(self.username.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}

    def _load(self):
        self.key = self.get_cache_key()
        self.now = self.timer()
        history = self.cache.get(self.key, [])
        self.history = [stamp for stamp in history if stamp > self.now - self.duration]

    def check(self):
        """Raise 429 if this username has failed too often lately."""
        if not self.username or self.rate is None:
            return
        self._load()
        if len(self.history) >= self.num_requests:
            raise Throttled(wait=self.wait())

    def record_failure(self):
        if not self.username or self.rate is None:
            return
        self._load()
        self.history.insert(0, self.now)
        self.cache.set(self.key, self.history, self.duration)

    def clear(self):
        """Forget the failures, after the owner has proved who they are."""
        if self.username:
            self.cache.delete(self.get_cache_key())
