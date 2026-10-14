from django.test import SimpleTestCase

from .local_moderation_policy import (
    Category, Finding, Outcome, Signal, ScreeningUnavailable, evaluate,
)


class LocalModerationPolicyTests(SimpleTestCase):
    def signal(self, category, finding=Finding.SUSPECTED):
        return Signal(category, finding, 'test-detector-v1', 'test-rule')

    def test_no_sexual_safety_findings_allows_publication(self):
        result = evaluate([], screening_complete=True)
        self.assertEqual(result.outcome, Outcome.ALLOW)
        self.assertTrue(result.publicly_visible)
        self.assertFalse(result.needs_review)

    def test_clear_prohibited_categories_block(self):
        for category in Category:
            with self.subTest(category=category):
                result = evaluate([self.signal(category, Finding.CLEAR)], screening_complete=True)
                self.assertEqual(result.outcome, Outcome.BLOCK)
                self.assertFalse(result.publicly_visible)

    def test_uncertain_adult_sexual_findings_remain_visible_for_review(self):
        for category in (Category.NUDITY, Category.EXPLICIT_SEXUAL):
            result = evaluate([self.signal(category)], screening_complete=True)
            self.assertEqual(result.outcome, Outcome.FLAG_VISIBLE)
            self.assertTrue(result.publicly_visible)
            self.assertTrue(result.needs_review)

    def test_child_safety_exception_overrides_visible_flags(self):
        result = evaluate([self.signal(Category.NUDITY),
                           self.signal(Category.CHILD_SEXUAL_EXPLOITATION)], screening_complete=True)
        self.assertEqual(result.outcome, Outcome.HOLD)
        self.assertFalse(result.publicly_visible)
        self.assertTrue(result.needs_review)

    def test_block_takes_precedence_over_hold_regardless_of_order(self):
        signals = [self.signal(Category.CHILD_SEXUAL_EXPLOITATION),
                   self.signal(Category.EXPLICIT_SEXUAL, Finding.CLEAR)]
        self.assertEqual(evaluate(signals, screening_complete=True),
                         evaluate(signals[::-1], screening_complete=True))
        self.assertEqual(evaluate(signals, screening_complete=True).outcome, Outcome.BLOCK)

    def test_missing_screening_is_never_allowed(self):
        for state in (False, None, 'true', 1):
            with self.assertRaises(ScreeningUnavailable):
                evaluate([], screening_complete=state)

    def test_untrusted_or_unbounded_signals_refused(self):
        for value in ([{'category': 'nudity'}], {}, [self.signal(Category.NUDITY)] * 257):
            with self.assertRaises(ValueError):
                evaluate(value, screening_complete=True)
        with self.assertRaises(ValueError):
            Signal('hate', Finding.CLEAR, 'v1', 'rule')
        with self.assertRaises(ValueError):
            Signal(Category.NUDITY, Finding.CLEAR, 'v1', 'raw user content')

    def test_duplicate_signals_do_not_duplicate_evidence(self):
        signal = self.signal(Category.NUDITY)
        self.assertEqual(len(evaluate([signal, signal], screening_complete=True).reasons), 1)
