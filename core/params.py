"""Query parameters, read as what they say they are.

A filter value goes into the ORM as a typed comparison, and Django refuses a
value that does not convert -- by raising, which uncaught is a 500 for what
is really a bad request. `?author=abc` on the post list, `?post=abc` on
comments, a planner range of `?start=bad` and five more did exactly that.

So a parameter that names a row or a day is parsed here, and refused with a
400 naming the parameter when it is not one.
"""

from datetime import date

from rest_framework.exceptions import ValidationError

#: The largest id any backend here can store. SQLite raises OverflowError on
#: anything bigger, before the query is even run.
MAX_ID = 2 ** 63 - 1


def id_param(request, name):
    """A positive whole number, or None when the parameter is absent."""
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "Must be a whole number."}) from None
    if not 1 <= value <= MAX_ID:
        raise ValidationError({name: "Must be a positive whole number."})
    return value


def date_param(request, name):
    """A calendar date in YYYY-MM-DD form, or None when absent."""
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "Use a date in YYYY-MM-DD form."}) from None
