"""The operator's cohort commands, and the experiment arithmetic with no data.

test_release covers stages and gates; test_learned_ranker covers experiments
with real numbers. These pin the operator paths nobody had run (inviting,
an unknown name, saying it twice) and what the statistics answer when there
is nothing, or not enough, to compare.
"""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from .experiment_stats import compare, needed_per_arm, p_value
from .fixtures_for_tests import FAST_PASSWORDS, make_account
from .models import SocialCohortMember


@FAST_PASSWORDS
class CohortCommandTests(TestCase):
    def setUp(self):
        _, self.profile, _ = make_account("cohort-member")

    def run_command(self, *args):
        out = StringIO()
        call_command("social_rollout", *args, stdout=out)
        return out.getvalue().strip()

    def test_inviting_adds_the_account_once(self):
        self.assertEqual(self.run_command("invite", "cohort-member"), "invited")
        self.assertEqual(self.run_command("invite", "cohort-member"), "already invited")
        self.assertEqual(SocialCohortMember.objects.filter(user=self.profile).count(), 1)

    def test_uninviting_someone_not_invited_says_so(self):
        self.assertEqual(self.run_command("uninvite", "cohort-member"), "was not invited")
        self.run_command("invite", "cohort-member")
        self.assertEqual(self.run_command("uninvite", "cohort-member"), "removed")
        self.assertFalse(SocialCohortMember.objects.exists())

    def test_an_unknown_username_is_an_error_and_changes_nothing(self):
        for action in ("invite", "uninvite"):
            with self.subTest(action=action):
                with self.assertRaisesMessage(CommandError, "No account with that username."):
                    self.run_command(action, "nobody-by-this-name")
        self.assertFalse(SocialCohortMember.objects.exists())


class ExperimentStatisticsEdgeTests(SimpleTestCase):
    def test_no_readers_needed_when_there_is_nothing_to_detect(self):
        for base in (None, 0, 0.0, -0.1, 1, 1.5):
            with self.subTest(base=base):
                self.assertIsNone(needed_per_arm(base))
        self.assertIsNone(needed_per_arm(0.2, relative_lift=0))
        self.assertIsNone(needed_per_arm(0.2, relative_lift=-0.5))

    def test_a_reasonable_base_rate_needs_a_finite_positive_sample(self):
        needed = needed_per_arm(0.2)
        self.assertIsInstance(needed, int)
        self.assertGreater(needed, 0)
        # A bigger lift is easier to see.
        self.assertLess(needed_per_arm(0.2, relative_lift=0.5), needed)

    def test_no_p_value_without_readers_or_without_spread(self):
        self.assertIsNone(p_value(0, 0, 5, 10))
        self.assertIsNone(p_value(5, 10, 0, 0))
        # Everyone in both arms did it: no variance, no test.
        self.assertIsNone(p_value(10, 10, 10, 10))
        self.assertIsNone(p_value(0, 10, 0, 10))

    def test_identical_arms_have_a_p_value_of_one(self):
        self.assertAlmostEqual(p_value(30, 100, 30, 100), 1.0)

    def test_an_empty_experiment_is_directional_with_nothing_claimed(self):
        result = compare(0, 0, 0, 0)
        self.assertEqual(result["claim"], "directional")
        self.assertFalse(result["powered"])
        self.assertIsNone(result["control_rate"])
        self.assertIsNone(result["rate"])
        self.assertIsNone(result["relative_lift"])
        self.assertIsNone(result["readers_needed_per_arm"])
        self.assertIsNone(result["p_value"])

    def test_a_control_where_nobody_acted_cannot_give_a_lift(self):
        result = compare(0, 500, 20, 500)
        self.assertEqual(result["control_rate"], 0.0)
        self.assertIsNone(result["relative_lift"])
        self.assertEqual(result["claim"], "directional")

    def test_an_underpowered_difference_hides_its_p_value(self):
        result = compare(2, 10, 6, 10)
        self.assertFalse(result["powered"])
        self.assertIsNone(result["p_value"])
        self.assertEqual(result["claim"], "directional")

    def test_a_powered_large_difference_is_significant(self):
        result = compare(2000, 10000, 3000, 10000)
        self.assertTrue(result["powered"])
        self.assertEqual(result["claim"], "significant")
        self.assertLess(result["p_value"], 0.05)
        self.assertAlmostEqual(result["relative_lift"], 0.5)

    def test_a_powered_null_result_is_no_difference_found(self):
        result = compare(2000, 10000, 2000, 10000)
        self.assertTrue(result["powered"])
        self.assertEqual(result["claim"], "no difference found")
