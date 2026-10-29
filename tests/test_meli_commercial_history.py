"""Pure audit merge tests; prior knowledge cannot authorize changed economics."""
import copy
import unittest
from types import SimpleNamespace
try:
    from . import test_meli_commercial_validation as fixtures
    from .test_meli_economic_import import operation
    from .test_meli_order_economics import economics, observation, order
except ImportError:
    import test_meli_commercial_validation as fixtures
    from test_meli_economic_import import operation
    from test_meli_order_economics import economics, observation, order


class EconomicHistoryTests(unittest.TestCase):
    def rows(self):
        fixture = fixtures.CommercialValidationTests()
        incomplete = fixture.interpret()
        complete = fixture.interpret(discounts=observation({"details": []}))
        return incomplete, complete

    def test_existing_snapshot_store_does_not_erase_prior_evidence(self):
        _incomplete, complete = self.rows()
        previous = copy.deepcopy(complete)
        previous["payments"][0]["net_received"] = "170"
        record = SimpleNamespace(ensure_one=lambda: None, order_id="123", currency_id="ARS",
                                 economic_snapshot=copy.deepcopy(previous))
        record.write = lambda values: setattr(record, "economic_snapshot", values["economic_snapshot"]) or True
        record._meli_write_economic_snapshot = lambda snapshot: record.write({"economic_snapshot": snapshot})
        operation("_meli_store_economic_snapshot")(record, complete)
        self.assertIn(previous, record.economic_snapshot.get("history", []))

    def test_transition_preserves_prior_observation_and_duplicate_is_noop(self):
        incomplete, complete = self.rows()
        merged = economics.merge_economic_evidence(incomplete, complete)
        self.assertIn(incomplete, merged["history"])
        self.assertEqual(merged["status"], "validated")
        self.assertEqual(economics.merge_economic_evidence(merged, copy.deepcopy(complete)), merged)

    def test_incomplete_refresh_keeps_confirmed_evidence_and_records_unknown(self):
        incomplete, complete = self.rows()
        previous = economics.merge_economic_evidence(incomplete, complete)
        refreshed = economics.merge_economic_evidence(previous, incomplete)
        self.assertEqual(refreshed["status"], "validated")
        self.assertIn(incomplete, refreshed["history"])
        self.assertEqual(refreshed["latest_observation"]["status"], "conditioned_draft")
        self.assertEqual(economics.merge_economic_evidence(refreshed, incomplete), refreshed)

    def test_changed_price_never_inherits_previous_promotional_completeness(self):
        incomplete, complete = self.rows()
        changed = copy.deepcopy(incomplete)
        changed["commercial_total"] = changed["invoice_product_basis"] = changed["buyer_paid"] = "199"
        changed["items"][0]["unit_price"] = "99.5"
        merged = economics.merge_economic_evidence(complete, changed)
        self.assertEqual(merged["status"], "conditioned_draft")
        self.assertEqual(merged["commercial_total"], "199")
        self.assertIsNone(merged["seller_funded"])
        self.assertIn(complete, merged["history"])

    def test_history_cannot_rebind_order_or_mutate_inputs(self):
        incomplete, complete = self.rows()
        before = copy.deepcopy((incomplete, complete))
        bad = dict(complete, order_id="other")
        with self.assertRaises(ValueError):
            economics.merge_economic_evidence(incomplete, bad)
        self.assertEqual((incomplete, complete), before)


if __name__ == "__main__":
    unittest.main()
