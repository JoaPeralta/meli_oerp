"""Approval retention in the actual economic merger, not fiscal/ORM proof."""
import copy
import unittest

try:
    from . import test_meli_commercial_validation as fixtures
    from .test_meli_order_economics import economics, observation
except ImportError:
    import test_meli_commercial_validation as fixtures
    from test_meli_order_economics import economics, observation


class ManualApprovalRetention(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CommercialValidationTests()
        self.conditioned = fixture.interpret()
        self.complete = fixture.interpret(discounts=observation({"details": []}))
        self.approval = {"schema": "viarengo.manual-approval/v1", "binding": "old-binding",
                         "operator_id": 19, "approved_at": "2026-10-29 12:00:00",
                         "reason": "Amounts reviewed", "evidence": "Owned order/payment review"}
        self.previous = dict(copy.deepcopy(self.conditioned), manual_approvals=[self.approval])

    def test_approval_is_retained_separately_from_changed_economic_truth(self):
        merged = economics.merge_economic_evidence(self.previous, self.complete)
        self.assertEqual(merged.get("manual_approvals"), [self.approval])
        self.assertEqual(merged["status"], "validated")
        self.assertTrue(merged["history"])
        self.assertTrue(all("manual_approvals" not in row for row in merged["history"]))
        self.assertNotIn("manual_approvals", merged["latest_observation"])

    def test_identical_observation_does_not_add_false_history_or_duplicate_approval(self):
        merged = economics.merge_economic_evidence(self.previous, self.conditioned)
        self.assertEqual(merged, self.previous)
        self.assertEqual(self.previous["manual_approvals"], [self.approval])


if __name__ == "__main__":
    unittest.main()
