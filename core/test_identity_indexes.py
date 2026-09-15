"""Do the account-identity indexes actually get used?

Adding an index and asserting the uniqueness it enforces is easy. Whether the
planner will use it for the lookup it was also meant to speed up is a
different question, and the answer depends on the shape of the index rather
than on the intent behind it -- a partial index can only serve a query
PostgreSQL can prove matches its predicate.

So this asks PostgreSQL. Sequential scans are disabled first, because on a
ten-row table a seq scan wins on cost and would hide whether the index is
usable at all; with it off, a plan that still refuses the index is proof the
index cannot serve that query.

SQLite skips these: it compiles __iexact to LIKE, not to UPPER(col) = UPPER(?),
so it is answering a different question.
"""

from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase

User = get_user_model()

EMAIL_INDEX = 'auth_user_email_upper_uniq'
USERNAME_INDEX = 'auth_user_username_upper_uniq'


@skipUnless(connection.vendor == 'postgresql', 'Planner behaviour is PostgreSQL-specific')
class TheIdentityIndexesAreUsedTests(TransactionTestCase):
    def setUp(self):
        User.objects.bulk_create([
            User(username=f'person{n}', email=f'person{n}@example.test')
            for n in range(200)
        ])

    def plan(self, queryset):
        sql, params = queryset.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute('SET LOCAL enable_seqscan = off')
            cursor.execute('EXPLAIN ' + sql, params)
            return '\n'.join(row[0] for row in cursor.fetchall())

    def test_a_username_lookup_uses_its_index(self):
        plan = self.plan(User.objects.filter(username__iexact='person7'))
        self.assertIn(USERNAME_INDEX, plan, f'username lookup did not use the index:\n{plan}')

    def test_an_email_lookup_uses_its_index(self):
        """The one in doubt. The email index is partial -- WHERE email <> '' --
        because two accounts here have no address and a plain unique index
        would refuse to apply. A partial index only serves a query whose
        predicate PostgreSQL can prove implies the index predicate, and
        UPPER(email) = UPPER(?) does not say anything about email being
        non-empty."""
        plan = self.plan(User.objects.filter(email__iexact='person7@example.test'))
        self.assertIn(EMAIL_INDEX, plan, f'email lookup did not use the index:\n{plan}')
