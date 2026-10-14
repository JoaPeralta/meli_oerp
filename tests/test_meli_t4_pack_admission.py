"""Standalone source checks; these are not native ORM acceptance evidence."""
import ast
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pack_admission", ROOT / "models" / "meli_pack_admission.py"
)
admission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission)


def order(oid=101, seller=42, pack=900, quantity=2, price=100, variation=7):
    return {
        "id": oid, "seller": {"id": seller}, "pack_id": pack,
        "status": "paid", "currency_id": "USD", "shipping": {"id": 800},
        "order_items": [{"item": {"id": "MLA123", "variation_id": variation},
                         "quantity": quantity, "unit_price": price,
                         "currency_id": "USD"}],
    }


class OrderAdmissionTests(unittest.TestCase):
    def test_order_without_shipping_is_valid(self):
        payload = order(pack=None)
        del payload["shipping"]
        self.assertEqual(admission.verify_order(payload, 42)["id"], 101)

    def test_snapshot_is_not_the_mutable_input(self):
        payload = order()
        result = admission.verify_order(payload, 42, expected_id=101)
        payload["order_items"][0]["quantity"] = 99
        self.assertEqual(result["order_items"][0]["quantity"], 2)

    def test_current_seller_is_required(self):
        payload = order()
        del payload["seller"]
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_foreign_order_is_rejected(self):
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(order(seller=43), 42)

    def test_order_identity_cannot_change(self):
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(order(102), 42, expected_id=101)

    def test_boolean_identity_is_not_an_integer_id(self):
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(order(True), 42)

    def test_missing_commercial_items_is_rejected(self):
        payload = order()
        payload["order_items"] = []
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_invalid_quantity_is_rejected(self):
        for quantity in (0, -1, True, 1.5):
            with self.subTest(quantity=quantity), self.assertRaises(admission.AdmissionError):
                admission.verify_order(order(quantity=quantity), 42)

    def test_nonfinite_price_is_rejected(self):
        for price in (float("nan"), float("inf"), -1):
            with self.subTest(price=price), self.assertRaises(admission.AdmissionError):
                admission.verify_order(order(price=price), 42)

    def test_nullable_variation_is_valid(self):
        self.assertIsNone(admission.verify_order(order(variation=None), 42)
                          ["order_items"][0]["item"]["variation_id"])

    def test_duplicate_commercial_item_identity_is_not_guessed(self):
        payload = order()
        payload["order_items"].append(deepcopy(payload["order_items"][0]))
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_bundle_parent_is_blocked(self):
        payload = order()
        payload["order_items"][0]["bundle"] = {"parent_item": {"id": "MLA9"}}
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_split_and_component_cancellation_are_blocked(self):
        for code in ("pack_splitted", "bundle_component"):
            payload = order()
            payload["cancel_detail"] = {"code": code}
            with self.subTest(code=code), self.assertRaises(admission.AdmissionError):
                admission.verify_order(payload, 42)

    def test_cancelled_child_is_pending_not_whole_pack_cancel(self):
        payload = order()
        payload["status"] = "cancelled"
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_missing_status_is_rejected(self):
        payload = order(pack=None)
        del payload["shipping"]
        del payload["status"]
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_empty_status_is_rejected(self):
        payload = order()
        payload["status"] = ""
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)

    def test_whitespace_status_is_rejected(self):
        for status in (" ", "\t\r\n"):
            payload = order()
            payload["status"] = status
            with self.subTest(status=status), self.assertRaises(admission.AdmissionError):
                admission.verify_order(payload, 42)

    def test_nonstring_status_is_rejected(self):
        for status in (None, False, 0, 123, [], {}):
            payload = order()
            payload["status"] = status
            with self.subTest(status=status), self.assertRaises(admission.AdmissionError):
                admission.verify_order(payload, 42)

    def test_status_guard_does_not_invent_provider_enumeration(self):
        payload = order()
        payload["status"] = "future_provider_state"
        self.assertEqual(admission.verify_order(payload, 42)["status"], "future_provider_state")

    def test_malformed_optional_objects_fail_closed(self):
        for key in ("shipping", "cancel_detail", "tags"):
            payload = order()
            payload[key] = "malformed"
            with self.subTest(key=key), self.assertRaises(admission.AdmissionError):
                admission.verify_order(payload, 42)

    def test_already_discounted_unit_price_is_preserved(self):
        payload = order(price=80)
        payload["order_items"][0]["gross_price"] = 100
        payload["order_items"][0]["discounts"] = [{"amount": 20}]
        self.assertEqual(admission.verify_order(payload, 42)["order_items"][0]["unit_price"], 80)

    def test_buyer_enrichment_cannot_replace_commercial_snapshot(self):
        current = order(quantity=2, price=100)
        refreshed = order(quantity=9, price=999)
        refreshed["buyer"] = {"id": 77, "first_name": "Synthetic"}
        result = admission.enrich_buyer(current, refreshed, 42)
        self.assertEqual(result["order_items"], current["order_items"])
        self.assertEqual(result["buyer"]["id"], 77)
        self.assertNotIn("buyer", current)

    def test_buyer_refresh_cannot_change_pack_membership(self):
        with self.assertRaises(admission.AdmissionError):
            admission.enrich_buyer(order(pack=None), order(pack=900), 42)

    def test_buyer_refresh_cannot_change_source_item(self):
        refreshed = order()
        refreshed["order_items"][0]["item"]["id"] = "MLA999"
        with self.assertRaises(admission.AdmissionError):
            admission.enrich_buyer(order(), refreshed, 42)

    def test_delivered_history_is_not_new_initial_admission(self):
        payload = order()
        payload["tags"] = ["delivered"]
        with self.assertRaises(admission.AdmissionError):
            admission.verify_order(payload, 42)


if __name__ == "__main__":
    unittest.main()
