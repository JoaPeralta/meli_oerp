"""Prepared ORM negatives for synthetic economic evidence; never real API calls."""
import copy
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .test_meli_order_economics import order, benefit, observation


@tagged("post_install", "-at_install", "meli_economic_evidence_negative")
class TestEconomicEvidenceNegative(TransactionCase):
    def setUp(self):
        super().setUp()
        self.original = {"schema": "synthetic-prior-audit/v1", "preserve": True}
        self.source = self.env["mercadolibre.orders"].create({
            "name": "Synthetic economic evidence negative", "order_id": "123", "currency_id": "ARS",
            "company_id": self.env.company.id, "seller_id": self.env.user.id,
            "economic_snapshot": copy.deepcopy(self.original)})

    def assert_rejected_without_audit_write(self, payload, discounts, expected):
        model = type(self.source)
        mp = observation({"id": 77, "status": "approved", "currency_id": "ARS", "collector_id": 7,
            "transaction_amount": 200, "coupon_amount": (payload.get("coupon") or {}).get("amount", 0),
            "shipping_amount": 0, "transaction_details": {"total_paid_amount": payload["paid_amount"]}})
        with patch.object(model, "_fetch_order_discounts", return_value=discounts), \
                patch.object(model, "_meli_fetch_payment_observation", return_value=mp) as fetch:
            result, payments = self.source._meli_economic_preflight(payload, object())
        self.assertEqual(result["reason"], expected)
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.source._meli_store_economic_snapshot(result)
        self.env.invalidate_all()
        self.assertEqual(self.source.economic_snapshot, self.original)
        return result, fetch

    def test_invalid_payment_identity_never_acquires_mp_or_replaces_prior_audit(self):
        payload = order()
        payload["payments"][0]["id"] = "not-a-payment"
        result, fetch = self.assert_rejected_without_audit_write(payload,
            observation({"details": []}), "payment_identity_invalid")
        fetch.assert_not_called()

    def test_nested_supplier_metadata_cannot_be_persisted_in_json_audit(self):
        b = benefit("cashback", 5, 0)
        b["supplier"]["campaign_id"] = {"access_token": "SYNTHETIC_SECRET"}
        result, fetch = self.assert_rejected_without_audit_write(order(),
            observation({"details": [b]}), "economic_metadata_invalid")
        self.assertEqual(result["benefits"], [])

    def test_contradictory_coupon_identity_preserves_original_orm_evidence(self):
        payload = order()
        payload["paid_amount"] = payload["payments"][0]["total_paid_amount"] = 180
        payload["coupon"] = {"id": "COUPON_A", "amount": 20}
        b = benefit("coupon")
        b["coupon"]["id"] = "COUPON_B"
        self.assert_rejected_without_audit_write(payload,
            observation({"details": [b]}), "coupon_identity_mismatch")
