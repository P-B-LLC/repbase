"""An index the email lookup can actually use.

0051 put one unique index on UPPER(email) and claimed it did two jobs:
enforce case-insensitive uniqueness, and serve the UPPER(col) = UPPER(?)
that sign-in, registration and password reset all compile to. It does the
first. It does not do the second, and the reason is the thing that made it
applicable at all.

That index is partial -- WHERE email <> '' -- because two accounts have no
address and a plain unique index would have refused to apply. PostgreSQL will
only use a partial index for a query it can prove matches the predicate, and
UPPER(email) = 'SOMEBODY@EXAMPLE.TEST' says nothing about email being
non-empty. Asked directly, with sequential scans disabled so a small table
could not hide the answer behind a cheaper plan, the planner still chose:

    Seq Scan on auth_user
      Filter: (upper((email)::text) = 'PERSON7@EXAMPLE.TEST'::text)

So the two jobs need two indexes. The partial unique one keeps two accounts
from sharing an address in different cases; this non-partial one is what the
lookup uses. The username index needed no equivalent -- it was never partial,
and its test passed on the same run.
"""

from django.db import migrations

LOOKUP_INDEX = "auth_user_email_upper"

CREATE = f"""
CREATE INDEX IF NOT EXISTS {LOOKUP_INDEX} ON auth_user (UPPER(email));
"""

DROP = f"""
DROP INDEX IF EXISTS {LOOKUP_INDEX};
"""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0052_workoutsession_route_summary"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunSQL(sql=CREATE, reverse_sql=DROP)]
