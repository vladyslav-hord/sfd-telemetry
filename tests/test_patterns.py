import unittest

from analyzer.patterns import candidate_state, robust_z, should_drift


class PatternTests(unittest.TestCase):
    def test_confirmation_requires_all_guards(self):
        self.assertEqual(candidate_state(12, 3, 3, .8, .75, .09), "active")
        self.assertEqual(candidate_state(12, 3, 3, .79, .75, .09), "candidate")

    def test_robust_z(self):
        self.assertGreater(robust_z(20, [1, 2, 2, 3, 3]), 4)

    def test_drift(self):
        self.assertTrue(should_drift(.7, 0, False))
