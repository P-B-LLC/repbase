"""Make the database agree with what the application already believes.

Registration refuses an email or username that matches an existing one
case-insensitively, and sign-in and password reset both look accounts up the
same way. None of that was written down anywhere the database could enforce
it, so two things were true at once:

- Uniqueness was a check followed by a commit. Between those two statements
  another request can commit the same address. SQLite hides this by allowing
  one writer at a time; PostgreSQL will not, and password reset then picks
  `.first()` of the match, which is an arbitrary one of the accounts.
- Every lookup was unindexed. `__iexact` compiles to `UPPER(col) = UPPER(?)`
  on PostgreSQL, and a plain B-tree index on the column cannot serve that, so
  sign-in, registration and password reset each scan the user table.

One unique index on the upper-cased column answers both.

Written as raw SQL because the model is `django.contrib.auth.models.User`,
which lives in an app this project does not own and cannot add a migration
to. A custom user model would be the tidier answer and is a much larger
change than this is worth.

The email index is partial. Two accounts here have no email at all --
superusers made with `createsuperuser` and no address -- and an empty string
is not a duplicate of another empty string in any sense worth enforcing. A
non-partial index would simply refuse to apply.

Checked against the live data before writing: ten accounts, no
case-duplicate emails, no case-duplicate usernames, two blank emails. If that
ever stops being true this migration fails loudly rather than corrupting
anything, which is the correct failure.
"""

from django.db import migrations

EMAIL_INDEX = "auth_user_email_upper_uniq"
USERNAME_INDEX = "auth_user_username_upper_uniq"

CREATE = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {EMAIL_INDEX}
    ON auth_user (UPPER(email)) WHERE email <> '';
CREATE UNIQUE INDEX IF NOT EXISTS {USERNAME_INDEX}
    ON auth_user (UPPER(username));
"""

DROP = f"""
DROP INDEX IF EXISTS {EMAIL_INDEX};
DROP INDEX IF EXISTS {USERNAME_INDEX};
"""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0050_index_expiry_columns"),
        # The table has to exist before an index can be put on it, and it
        # belongs to another app, so say so rather than rely on ordering.
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunSQL(sql=CREATE, reverse_sql=DROP)]
