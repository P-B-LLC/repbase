"""Whether an experiment's difference is one that can be claimed.

Readers are assigned to an arm, so the reader is the unit: one person who
likes forty posts is one observation, not forty. The figures here are about
the share of readers in an arm who did something at least once.

A small beta will almost never have the numbers for a claim. This says so
plainly -- `directional` -- instead of printing a p-value that invites one.
"""

import math
from statistics import NormalDist

ALPHA = 0.05
POWER = 0.8
#: The smallest relative lift worth detecting when none is given: a tenth.
RELATIVE_LIFT = 0.1

_normal = NormalDist()


def needed_per_arm(base_rate, relative_lift=RELATIVE_LIFT, alpha=ALPHA, power=POWER):
    """Readers each arm needs to detect `relative_lift` over `base_rate`,
    two-sided. None when the base rate leaves nothing to detect."""
    if not base_rate or base_rate <= 0 or base_rate >= 1 or relative_lift <= 0:
        return None
    other = min(base_rate * (1 + relative_lift), 0.999999)
    if other == base_rate:
        return None
    z_alpha = _normal.inv_cdf(1 - alpha / 2)
    z_power = _normal.inv_cdf(power)
    mean = (base_rate + other) / 2
    top = (
        z_alpha * math.sqrt(2 * mean * (1 - mean))
        + z_power * math.sqrt(base_rate * (1 - base_rate) + other * (1 - other))
    ) ** 2
    return math.ceil(top / (other - base_rate) ** 2)


def p_value(successes_a, n_a, successes_b, n_b):
    """Two-sided p for a difference between two shares; None without data."""
    if not n_a or not n_b:
        return None
    pooled = (successes_a + successes_b) / (n_a + n_b)
    spread = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if spread == 0:
        return None
    z = (successes_b / n_b - successes_a / n_a) / spread
    return 2 * (1 - _normal.cdf(abs(z)))


def compare(control_successes, control_n, arm_successes, arm_n, relative_lift=RELATIVE_LIFT):
    """An arm against the control, with the only claim the numbers allow.

    - `significant`: enough readers in both arms to have detected a lift of
      `relative_lift`, and the difference is unlikely to be chance.
    - `no difference found`: enough readers, and no such difference.
    - `directional`: too few readers to say. Read it beside interviews, and
      do not report it as an improvement.
    """
    base = control_successes / control_n if control_n else None
    arm = arm_successes / arm_n if arm_n else None
    needed = needed_per_arm(base, relative_lift) if base is not None else None
    powered = needed is not None and control_n >= needed and arm_n >= needed
    p = p_value(control_successes, control_n, arm_successes, arm_n)
    if not powered:
        claim = "directional"
    elif p is not None and p < ALPHA:
        claim = "significant"
    else:
        claim = "no difference found"
    return {
        "control_rate": None if base is None else round(base, 4),
        "rate": None if arm is None else round(arm, 4),
        "relative_lift": None if not base or arm is None else round(arm / base - 1, 4),
        "readers_needed_per_arm": needed,
        "powered": powered,
        "p_value": None if p is None or not powered else round(p, 4),
        "claim": claim,
    }
