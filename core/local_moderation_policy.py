"""Provider-independent policy for a future self-hosted moderation pipeline.

This module is intentionally not a detector and is not enabled on publication
paths yet. Its inputs must come from trusted server-side, validated detectors,
never client-supplied labels. A clear signal means a calibrated detector finding,
not a claim of legal certainty. No content or personal data is logged here.
"""
from dataclasses import dataclass
from enum import StrEnum


POLICY_VERSION = 'sexual-safety-2026-10-14'


class Category(StrEnum):
    NUDITY = 'nudity'
    EXPLICIT_SEXUAL = 'explicit_sexual'
    CHILD_SEXUAL_EXPLOITATION = 'child_sexual_exploitation'


class Finding(StrEnum):
    CLEAR = 'clear'
    SUSPECTED = 'suspected'


class Outcome(StrEnum):
    ALLOW = 'allow'
    FLAG_VISIBLE = 'flag_visible'
    HOLD = 'hold'
    BLOCK = 'block'


@dataclass(frozen=True)
class Signal:
    category: Category
    finding: Finding
    detector_version: str
    rule_id: str

    def __post_init__(self):
        if not isinstance(self.category, Category) or not isinstance(self.finding, Finding):
            raise ValueError('Unknown policy category or finding')
        for label in (self.detector_version, self.rule_id):
            if (not isinstance(label, str) or not 1 <= len(label) <= 100
                    or any(not (c.isascii() and (c.isalnum() or c in '._:-')) for c in label)):
                raise ValueError('Evidence must use bounded identifiers, not raw content')


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reasons: tuple[Signal, ...]
    policy_version: str = POLICY_VERSION

    @property
    def publicly_visible(self):
        return self.outcome in (Outcome.ALLOW, Outcome.FLAG_VISIBLE)

    @property
    def needs_review(self):
        return self.outcome in (Outcome.FLAG_VISIBLE, Outcome.HOLD)


class ScreeningUnavailable(Exception):
    """Missing/failed required screening must not be converted into ALLOW."""


def evaluate(signals, *, screening_complete):
    """Evaluate only after every required text/image/video screening completes.

    Uncertain sexual content is visible pending review. Suspected exploitation
    of minors is always held. Clear prohibited content cannot be published.
    Viewpoints, profanity and hate are deliberately absent from blocking rules.
    An outage isn't an uncertain finding: it is an incomplete screening job.
    """
    if screening_complete is not True:
        raise ScreeningUnavailable('Required safety screening has not completed')
    if not isinstance(signals, (list, tuple)) or len(signals) > 256:
        raise ValueError('Expected a bounded collection of detector signals')
    if any(not isinstance(signal, Signal) for signal in signals):
        raise ValueError('Only validated server-side signals are accepted')
    reasons = tuple(sorted(set(signals), key=lambda s: (
        s.category, s.finding, s.detector_version, s.rule_id)))
    if any(s.finding == Finding.CLEAR for s in reasons):
        return Decision(Outcome.BLOCK, reasons)
    if any(s.category == Category.CHILD_SEXUAL_EXPLOITATION for s in reasons):
        return Decision(Outcome.HOLD, reasons)
    if reasons:
        return Decision(Outcome.FLAG_VISIBLE, reasons)
    return Decision(Outcome.ALLOW, reasons)
