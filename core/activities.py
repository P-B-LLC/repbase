"""What somebody can say they did: the activities a workout can be.

`WorkoutTemplate.workout_type` was the only word for this, and it has four
values -- lifting, running, biking, swimming -- that every app build already
decodes as a closed enum. Tennis is not one of them, and it cannot become one:
the generated Swift client decodes a response enum strictly, so the first
tennis workout sent to an older build would fail that build's whole workout
list. So the four stay exactly as they are, as a legacy *tracking family*, and
the activity travels beside them as a plain string, which an old build ignores
and a new one can read without the server having to ship a client first.

Every activity names the family an old build should draw it as. Tennis is
`lifting` there, which is wrong in the abstract and right in practice: that is
the family whose screens an old build shows for a session with no route, and
the old build has no better word to pick.

The profile is what the server does with a session of that activity:

* `strength` -- sets-based. A session counts as trained only when it logged a
  set, exactly as every lifting session always has.
* `distance` -- a distance covered and the time it took.
* `session` -- just the time it took.

A distance or session activity counts as trained when its duration or its
distance is above zero; there are no sets to look for, and a tennis match with
no sets in it has still happened.

`activity_details` is the handful of activity-specific values a person may
type in (rounds of a boxing session, the style of a yoga class). Each activity
lists the keys it accepts; nothing else is stored. They are the person's own
words for what they did, never a measurement.

Nothing here imports the models: the models, the serializers and migrations
all read this, and a migration must not pull in the live model classes.
"""

from dataclasses import dataclass, field

STRENGTH = "strength"
DISTANCE = "distance"
SESSION = "session"

#: The two profiles counted by time or distance rather than by sets.
TIMED_PROFILES = frozenset({DISTANCE, SESSION})

#: The legacy families, which are `WorkoutTemplate.WorkoutType`'s values.
#: Spelled out rather than imported, for the reason in the module docstring.
LIFTING = "lifting"
RUNNING = "running"
BIKING = "biking"
SWIMMING = "swimming"


@dataclass(frozen=True)
class Choice:
    """A detail that is one of a fixed set of words."""

    values: tuple

    def clean(self, value):
        if not isinstance(value, str) or value not in self.values:
            raise ValueError(f"Use one of: {', '.join(self.values)}.")
        return value


@dataclass(frozen=True)
class Whole:
    """A detail that is a whole number within a range.

    `True` is not a number here. Python counts a bool as an int, and so would
    an `isinstance` check, so a JSON `true` would otherwise be stored as one
    round of boxing.
    """

    low: int
    high: int

    def clean(self, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Use a whole number from {self.low} to {self.high}.")
        if not self.low <= value <= self.high:
            raise ValueError(f"Use a whole number from {self.low} to {self.high}.")
        return value


@dataclass(frozen=True)
class Text:
    """A detail written in the person's own words, trimmed."""

    max_length: int

    def clean(self, value):
        if not isinstance(value, str):
            raise ValueError("Use text.")
        value = value.strip()
        if len(value) > self.max_length:
            raise ValueError(f"Use at most {self.max_length} characters.")
        return value


SESSION_TYPE = Choice(("match", "training", "casual"))
CROSSFIT_FORMAT = Choice(("amrap", "emom", "for_time", "tabata", "rounds", "chipper", "other"))
ROUNDS = Whole(0, 999)
REPS = Whole(0, 9999)
INTERVALS = Whole(0, 999)
ROUTES = Whole(0, 999)
ELEVATION_GAIN_M = Whole(0, 10000)
POOL_LENGTH_M = Whole(10, 100)
MOVEMENTS = Text(500)

YOGA_STYLE = Choice(("vinyasa", "hatha", "yin", "power", "restorative", "hot", "other"))
PILATES_STYLE = Choice(("mat", "reformer", "other"))
MARTIAL_ARTS_STYLE = Choice((
    "karate", "taekwondo", "judo", "jiu_jitsu", "muay_thai", "kung_fu",
    "wrestling", "mma", "other",
))
CLIMBING_STYLE = Choice(("bouldering", "top_rope", "lead", "other"))


@dataclass(frozen=True)
class Activity:
    slug: str
    #: The legacy `workout_type` an older app build draws this as.
    family: str
    profile: str
    #: The `activity_details` keys this activity accepts, each with its rule.
    details: dict = field(default_factory=dict)

    @property
    def counts_by_time(self):
        """Whether a session of this is trained by time or distance, not sets."""
        return self.profile in TIMED_PROFILES


def _activity(slug, family, profile, **details):
    return slug, Activity(slug=slug, family=family, profile=profile, details=details)


_RACKET = ("tennis", "padel", "badminton", "table_tennis", "squash", "pickleball")
_TEAM = (
    "soccer", "american_football", "basketball", "volleyball", "rugby",
    "hockey", "baseball", "handball", "cricket",
)

#: Every activity, slug to spec, in the order a picker should offer them.
#:
#: A plain dict, which keeps insertion order. The slugs are a wire format:
#: they are stored on workouts and sent to every client, so a slug is never
#: renamed or removed once shipped. Adding one is safe -- it travels as a
#: plain string -- but an app build that predates it will show it however that
#: build shows an activity it does not know.
ACTIVITIES = dict(
    [
        _activity("weightlifting", LIFTING, STRENGTH),
        _activity("calisthenics", LIFTING, STRENGTH),
        _activity(
            "crossfit", LIFTING, SESSION,
            format=CROSSFIT_FORMAT, rounds=ROUNDS, reps=REPS, movements=MOVEMENTS,
        ),
        _activity("circuit_training", LIFTING, SESSION, rounds=ROUNDS),
        _activity("functional_training", LIFTING, SESSION),
        _activity("hiit", LIFTING, SESSION, rounds=ROUNDS),
        _activity("running", RUNNING, DISTANCE),
        _activity("jogging", RUNNING, DISTANCE),
        _activity("sprinting", RUNNING, SESSION, intervals=INTERVALS),
        _activity("trail_running", RUNNING, DISTANCE, elevation_gain_m=ELEVATION_GAIN_M),
        _activity("walking", RUNNING, DISTANCE),
        _activity("hiking", RUNNING, DISTANCE, elevation_gain_m=ELEVATION_GAIN_M),
        _activity("cycling", BIKING, DISTANCE, elevation_gain_m=ELEVATION_GAIN_M),
        _activity("indoor_cycling", BIKING, DISTANCE),
        _activity("mountain_biking", BIKING, DISTANCE, elevation_gain_m=ELEVATION_GAIN_M),
        _activity("swimming", SWIMMING, DISTANCE, pool_length_m=POOL_LENGTH_M),
        _activity("open_water_swimming", SWIMMING, DISTANCE),
        _activity("rowing", LIFTING, DISTANCE),
        _activity("indoor_rowing", LIFTING, DISTANCE),
        _activity("paddling", LIFTING, DISTANCE),
        _activity("elliptical", LIFTING, SESSION),
        _activity("stair_climbing", LIFTING, SESSION),
        _activity("jump_rope", LIFTING, SESSION),
        *(_activity(slug, LIFTING, SESSION, session_type=SESSION_TYPE) for slug in _RACKET),
        *(_activity(slug, LIFTING, SESSION, session_type=SESSION_TYPE) for slug in _TEAM),
        _activity("boxing", LIFTING, SESSION, rounds=ROUNDS),
        _activity("kickboxing", LIFTING, SESSION, rounds=ROUNDS),
        _activity("martial_arts", LIFTING, SESSION, style=MARTIAL_ARTS_STYLE, rounds=ROUNDS),
        _activity("yoga", LIFTING, SESSION, style=YOGA_STYLE),
        _activity("pilates", LIFTING, SESSION, style=PILATES_STYLE),
        _activity("stretching", LIFTING, SESSION),
        _activity("dance", LIFTING, SESSION),
        _activity("climbing", LIFTING, SESSION, style=CLIMBING_STYLE, routes=ROUTES),
        _activity("skiing", LIFTING, SESSION),
        _activity("snowboarding", LIFTING, SESSION),
        _activity("surfing", LIFTING, SESSION),
        _activity("golf", LIFTING, SESSION),
        _activity("other", LIFTING, SESSION),
    ]
)

#: The activity a legacy `workout_type` means when nothing more specific was
#: said: what every workout written before activities existed is backfilled
#: to, and what an older app build's `workout_type` alone is read as.
LEGACY_DEFAULT_ACTIVITY = {
    LIFTING: "weightlifting",
    RUNNING: "running",
    BIKING: "cycling",
    SWIMMING: "swimming",
}

#: What a workout is when nobody said anything at all.
DEFAULT_ACTIVITY = LEGACY_DEFAULT_ACTIVITY[LIFTING]

#: Slugs grouped by how a session of them is counted, for SQL filters.
TIMED_ACTIVITIES = tuple(slug for slug, spec in ACTIVITIES.items() if spec.counts_by_time)

#: The families whose legacy default counts by time or distance. Only used for
#: a workout whose activity is somehow blank or unknown, so it is counted the
#: way its `workout_type` always was.
TIMED_FAMILIES = tuple(
    family for family, slug in LEGACY_DEFAULT_ACTIVITY.items()
    if ACTIVITIES[slug].counts_by_time
)


def activity_for(activity, workout_type=None):
    """The activity spec a workout stands for, or None.

    The workout's own activity when it is one this server knows; otherwise
    the legacy default of its `workout_type`; otherwise None, which is a
    session with no workout at all.
    """
    if activity in ACTIVITIES:
        return ACTIVITIES[activity]
    default = LEGACY_DEFAULT_ACTIVITY.get(workout_type)
    return ACTIVITIES[default] if default else None


def counts_by_time(activity, workout_type=None):
    """Whether a session is trained by time or distance rather than by sets.

    False for a session whose workout was deleted, which is how such a
    session has always been counted: by its sets.
    """
    spec = activity_for(activity, workout_type)
    return spec is not None and spec.counts_by_time


def session_trained(*, activity, workout_type, logged_sets, duration_seconds, distances):
    """Whether one finished session counts as training.

    `distances` is every distance the session holds, measured or typed; any
    of them above zero is a distance covered.
    """
    if counts_by_time(activity, workout_type):
        return (duration_seconds or 0) > 0 or any((value or 0) > 0 for value in distances)
    return (logged_sets or 0) > 0


def label(slug):
    """How an activity reads in an error message: "table tennis"."""
    return slug.replace("_", " ")


class ActivityDetailsError(ValueError):
    """Details that do not fit their activity, each problem keyed by name."""

    def __init__(self, errors):
        super().__init__(errors)
        self.errors = errors


def clean_activity_details(activity, details):
    """The details as they should be stored, or ActivityDetailsError.

    Every key is optional, and a key sent as null is the same as one left
    out, so a client can clear a value by nulling it. A key the activity does
    not take is refused rather than dropped, because a client that sends
    one has a different idea of the activity than the server does, and
    quietly losing what the person typed is the worse outcome. A session with
    no workout has no activity, and so takes no details.
    """
    if not isinstance(details, dict):
        raise ActivityDetailsError({"activity_details": "Use an object."})
    spec = ACTIVITIES.get(activity)
    allowed = spec.details if spec is not None else {}
    cleaned = {}
    errors = {}
    for key, value in details.items():
        rule = allowed.get(key)
        if rule is None:
            if activity:
                errors[key] = f"Not a detail of {label(activity)}."
            else:
                errors[key] = "This session has no activity to describe."
            continue
        if value is None:
            continue
        try:
            value = rule.clean(value)
        except ValueError as problem:
            errors[key] = str(problem)
            continue
        # Text that trims to nothing says nothing, the same as null.
        if value != "":
            cleaned[key] = value
    if errors:
        raise ActivityDetailsError(errors)
    return cleaned
