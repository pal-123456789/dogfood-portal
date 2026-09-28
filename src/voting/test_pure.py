# src/voting/test_pure.py
"""DB-free unit tests for the community-voting pure functions (SimpleTestCase, no database).

Covers the quadratic cost/budget check, the Gmail-aware duplicate-detection normalization, and the
deterministic per-voter ballot ordering -- the three pieces the feature spec calls out as pure and
directly testable.
"""
from collections import namedtuple

from django.test import SimpleTestCase

from voting import services

_Sub = namedtuple("_Sub", ["ext_id"])


class QuadraticBudgetTests(SimpleTestCase):
    def test_cost_is_square_of_votes(self):
        self.assertEqual(services.vote_cost(0), 0)
        self.assertEqual(services.vote_cost(1), 1)
        self.assertEqual(services.vote_cost(5), 25)

    def test_within_budget_boundary(self):
        # 5 votes on one project spends exactly the 25-credit budget -> allowed.
        self.assertTrue(services.within_budget([5], 25))
        # 5**2 + 1**2 = 26 credits -> over the 25 budget.
        self.assertFalse(services.within_budget([5, 1], 25))

    def test_total_credits_sums_squares(self):
        self.assertEqual(services.total_credits([3, 4]), 25)  # 9 + 16

    def test_clean_allocations_drops_zero_and_restricts(self):
        cleaned = services.clean_allocations(
            {"prj_01": "2", "prj_02": "0", "prj_99": "3", "csrfmiddlewaretoken": "x"},
            ["prj_01", "prj_02"])
        self.assertEqual(cleaned, {"prj_01": 2})  # zero dropped, off-ballot prj_99 ignored

    def test_clean_allocations_rejects_negative_and_nonint(self):
        with self.assertRaises(ValueError):
            services.clean_allocations({"prj_01": "-1"}, ["prj_01"])
        with self.assertRaises(ValueError):
            services.clean_allocations({"prj_01": "two"}, ["prj_01"])


class EmailNormalizationTests(SimpleTestCase):
    def test_gmail_folds_dots_and_plus(self):
        self.assertEqual(services.normalize_email("a.b+x@gmail.com"), "ab@gmail.com")
        self.assertEqual(services.normalize_email("ab@gmail.com"), "ab@gmail.com")
        self.assertEqual(
            services.normalize_email("a.b+x@gmail.com"),
            services.normalize_email("ab@gmail.com"))

    def test_googlemail_folds_onto_gmail(self):
        self.assertEqual(services.normalize_email("A.B@googlemail.com"), "ab@gmail.com")

    def test_other_domain_drops_plus_but_keeps_dots(self):
        # Plus-addressing is stripped for every domain; dots are preserved outside gmail.
        self.assertEqual(services.normalize_email("foo+bar@other.com"), "foo@other.com")
        self.assertEqual(services.normalize_email("f.o.o@other.com"), "f.o.o@other.com")

    def test_trim_and_lowercase(self):
        self.assertEqual(services.normalize_email("  Foo@Other.COM "), "foo@other.com")

    def test_no_at_sign_is_just_trimmed_lowercased(self):
        self.assertEqual(services.normalize_email("  PlainString "), "plainstring")


class BallotOrderTests(SimpleTestCase):
    subs = [_Sub("prj_0%d" % i) for i in range(1, 7)]

    def test_stable_for_same_voter(self):
        a = services.ballot_order("vcmp_demo", "user:1", self.subs)
        b = services.ballot_order("vcmp_demo", "user:1", self.subs)
        self.assertEqual([s.ext_id for s in a], [s.ext_id for s in b])

    def test_differs_across_voters(self):
        a = services.ballot_order("vcmp_demo", "user:1", self.subs)
        b = services.ballot_order("vcmp_demo", "user:2", self.subs)
        self.assertNotEqual([s.ext_id for s in a], [s.ext_id for s in b])

    def test_not_plain_id_order(self):
        # The shuffle must not simply return the input (id) order for every voter.
        ordered = services.ballot_order("vcmp_demo", "user:1", self.subs)
        self.assertNotEqual([s.ext_id for s in ordered], [s.ext_id for s in self.subs])
