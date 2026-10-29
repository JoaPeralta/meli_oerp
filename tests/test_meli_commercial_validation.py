"""Test-first commercial evidence contract; no HTTP/ORM or live provider fixtures."""
import copy
import json
import unittest

try:
    from .test_meli_order_economics import economics, observation, order
    from . import test_meli_economic_import as economic_import
except ImportError:
    from test_meli_order_economics import economics, observation, order
    import test_meli_economic_import as economic_import


EXACT_404 = observation({"error": "discount_not_found"}, 404)


def payment(payload, **changes):
    row = payload["payments"][0]
    body = {"id": row["id"], "status": "approved", "currency_id": payload["currency_id"],
            "collector_id": payload["seller"]["id"], "transaction_amount": row["transaction_amount"],
            "coupon_amount": (payload.get("coupon") or {}).get("amount", 0), "shipping_amount": 0,
            "transaction_details": {"total_paid_amount": row["total_paid_amount"]}}
    body.update(changes)
    return observation(body)


class CommercialValidationTests(unittest.TestCase):
    def interpret(self, payload=None, discounts=None, observations=None):
        payload = order() if payload is None else payload
        if observations is None:
            observations = {str(payload["payments"][0]["id"]): payment(payload)}
        return economics.interpret_order_economics(
            payload, EXACT_404 if discounts is None else discounts, observations)

    def assert_conditioned(self, result, total="200", paid="200"):
        self.assertEqual(result.get("status"), "conditioned_draft", result)
        self.assertEqual(result.get("commercial_status"), "verified", result)
        self.assertEqual(result.get("promotional_status"), "unknown", result)
        self.assertEqual((result.get("commercial_total"), result.get("invoice_product_basis"),
                          result.get("buyer_paid")), (total, total, paid))
        for key in ("benefits", "seller_funded", "marketplace_funded"):
            self.assertIsNone(result.get(key), key)
        self.assertEqual(result.get("discounts_http_status"), 404)
        self.assertEqual(result.get("discounts_error"), "discount_not_found")
        self.assertTrue(result.get("schema"))
        self.assertEqual(len(result.get("payments", [])), 1)

    def test_original_sanitized_ars1000_evidence_is_conditioned_not_zero_benefits(self):
        payload = order()
        payload.update(id=2000019073718792, total_amount=1000, paid_amount=1000)
        payload["order_items"][0].update(quantity=1, unit_price=1000, gross_price=1000, discounts=None)
        payload["payments"][0].update(transaction_amount=1000, total_paid_amount=1000)
        self.assert_conditioned(self.interpret(payload), "1000", "1000")

    def test_exact404_reference_price_gap_keeps_effective_price_without_funding_inference(self):
        payload = order()
        payload["order_items"][0]["gross_price"] = 240
        result = self.interpret(payload)
        self.assert_conditioned(result)
        self.assertEqual(result["items"][0]["reference_total"], "240")
        self.assertEqual(result["items"][0]["unit_price"], "100")

    def test_exact404_coupon_explains_buyer_difference_without_assigning_sponsor(self):
        payload = order()
        payload["coupon"] = {"id": "observed-coupon", "amount": 20}
        payload["paid_amount"] = payload["payments"][0]["total_paid_amount"] = 180
        result = self.interpret(payload)
        self.assert_conditioned(result, paid="180")
        self.assertEqual(result["payments"][0]["coupon_amount"], "20")
        self.assertEqual(result["order_coupon_id"], "observed-coupon")

    def test_exact404_payment_contradiction_outranks_promotional_incompleteness(self):
        payload = order()
        result = self.interpret(payload, observations={"77": payment(payload, transaction_amount=199)})
        self.assertEqual((result.get("status"), result.get("reason")),
                         ("contradictory", "payment_transaction_mismatch"))
        self.assertEqual(result.get("commercial_status"), "contradictory")
        self.assertEqual(result.get("promotional_status"), "unknown")

    def test_exact404_missing_mp_population_never_uses_order_only_scalars(self):
        for observations in ({}, {"77": observation({}, 503)}):
            with self.subTest(observations=observations):
                result = self.interpret(observations=observations)
                self.assertNotIn(result["status"], ("validated", "conditioned_draft"))
                self.assertNotEqual(result.get("commercial_status"), "verified")
        result = economics.interpret_order_economics(order(), EXACT_404)
        self.assertNotIn(result["status"], ("validated", "conditioned_draft"))

    def test_exact404_currency_collector_status_and_paid_contradictions_remain_blocked(self):
        for changes, reason in (
            ({"currency_id": "USD"}, "payment_identity_mismatch"),
            ({"collector_id": 8}, "payment_collector_mismatch"),
            ({"status": "rejected"}, "payment_identity_mismatch"),
            ({"transaction_details": {"total_paid_amount": 199}}, "payment_paid_mismatch"),
        ):
            with self.subTest(reason=reason):
                payload = order()
                result = self.interpret(payload, observations={"77": payment(payload, **changes)})
                self.assertEqual(result.get("reason"), reason)
                self.assertNotIn(result["status"], ("validated", "conditioned_draft"))

    def test_exact404_coupon_mismatch_and_unexplained_difference_are_not_balanced(self):
        payload = order()
        payload["coupon"] = {"amount": 20}
        payload["paid_amount"] = payload["payments"][0]["total_paid_amount"] = 180
        result = self.interpret(payload, observations={"77": payment(payload, coupon_amount=0)})
        self.assertEqual(result.get("reason"), "payment_coupon_mismatch")
        payload = order()
        payload["paid_amount"] = payload["payments"][0]["total_paid_amount"] = 199
        result = self.interpret(payload)
        self.assertEqual(result.get("reason"), "buyer_payment_difference_unexplained")

    def test_exact404_incomplete_coupon_or_shipping_observation_is_not_zero(self):
        payload = order()
        for field in ("coupon_amount", "shipping_amount"):
            observed = payment(payload)
            del observed["body"][field]
            with self.subTest(field=field):
                result = self.interpret(payload, observations={"77": observed})
                self.assertNotIn(result["status"], ("validated", "conditioned_draft"))
                self.assertNotEqual(result.get("commercial_status"), "verified")

    def test_exact404_unsupported_pack_unpaid_or_assigned_shipment_is_not_conditioned(self):
        for changes in ({"pack_id": 99}, {"status": "cancelled"}, {"shipping": {"id": 88}}):
            payload = order()
            payload.update(changes)
            with self.subTest(changes=changes):
                self.assertNotEqual(self.interpret(payload)["status"], "conditioned_draft")

    def test_transport_admission_is_exact_and_missing_response_is_not_accepted(self):
        responses = [observation({"error": "discount_not_found"}, status) for status in (401, 403, 503)]
        responses += [observation({"error": "not_found"}, 404),
                      observation({"error": "discount_not_found "}, 404),
                      observation(None, 404), observation({"error": "discount_not_found"}, 404, True)]
        for response in responses:
            with self.subTest(response=response):
                self.assertNotIn(self.interpret(discounts=response)["status"], ("validated", "conditioned_draft"))

    def test_200_empty_details_is_complete_but_still_requires_commercial_validation(self):
        result = self.interpret(discounts=observation({"details": []}))
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(result.get("commercial_status"), "verified")
        self.assertEqual(result.get("promotional_status"), "complete")
        self.assertEqual((result["benefits"], result["seller_funded"], result["marketplace_funded"]),
                         ([], "0", "0"))

    def test_200_incomplete_details_never_reuses_the_special404_route(self):
        for body in ({}, {"details": None}, {"details": "unknown"}, {"error": "discount_not_found"}):
            with self.subTest(body=body):
                self.assertNotIn(self.interpret(discounts=observation(body))["status"],
                                 ("validated", "conditioned_draft"))

    def test_unknown_result_is_pure_deterministic_and_excludes_private_payment_payloads(self):
        payload = order()
        observed = payment(payload)
        observed["body"]["payer"] = {"email": "private@example.test"}
        observed["body"]["charges_details"] = [{"type": "coupon", "amounts": {"original": 900}}]
        before = copy.deepcopy((payload, observed, EXACT_404))
        first = self.interpret(payload, observations={"77": observed})
        self.assert_conditioned(first)
        self.assertEqual(first, self.interpret(payload, observations={"77": observed}))
        self.assertEqual((payload, observed, EXACT_404), before)
        self.assertNotIn("private@example.test", json.dumps(first))
        self.assertNotIn("charges_details", json.dumps(first))


class CommercialAcquisitionTests(unittest.TestCase):
    producer = economic_import.EconomicImportTests.producer

    def test_exact404_acquires_independent_mp_and_reuses_its_actual_observation(self):
        model = self.producer(discounts=copy.deepcopy(EXACT_404))
        result, observations = model._meli_economic_preflight(order(), object())
        self.assertEqual(model.calls, ["discounts", ("payment", "77")])
        self.assertEqual(set(observations), {"77"})
        self.assertEqual(result, economics.interpret_order_economics(order(), EXACT_404, observations))
        self.assertEqual(result.get("status"), "conditioned_draft", result)

    def test_exact404_validates_entire_payment_population_before_acquiring_any_prefix(self):
        for bad in (None, False, "not-a-payment", "9" * 129):
            payload = order()
            payload["payments"].append(dict(payload["payments"][0], id=bad))
            model = self.producer(discounts=copy.deepcopy(EXACT_404))
            result, observed = model._meli_economic_preflight(payload, object())
            with self.subTest(bad=bad):
                self.assertEqual(model.calls, ["discounts"])
                self.assertEqual(observed, {})
                self.assertEqual(result.get("reason"), "payment_identity_invalid")

    def test_exact404_multiple_payments_are_complete_and_duplicate_ids_do_not_fetch(self):
        payload = order()
        payload["order_items"][0].update(quantity=4, gross_price=400)
        payload["total_amount"] = payload["paid_amount"] = 400
        payload["payments"].append(dict(payload["payments"][0], id=88))
        model = self.producer(discounts=copy.deepcopy(EXACT_404))
        result, observed = model._meli_economic_preflight(payload, object())
        self.assertEqual(model.calls, ["discounts", ("payment", "77"), ("payment", "88")])
        self.assertEqual(set(observed), {"77", "88"})
        self.assertEqual(result.get("status"), "conditioned_draft")
        payload["payments"][1]["id"] = "077"
        model = self.producer(discounts=copy.deepcopy(EXACT_404))
        result, observed = model._meli_economic_preflight(payload, object())
        self.assertEqual(model.calls, ["discounts"])
        self.assertEqual(result.get("reason"), "payment_identity_ambiguous")

    def test_other_transport_errors_never_trigger_the_conditional_acquisition_policy(self):
        for status, body in ((401, {}), (403, {}), (503, {}), (404, {"error": "other_error"})):
            model = self.producer(discounts=observation(body, status))
            result, observed = model._meli_economic_preflight(order(), object())
            with self.subTest(status=status):
                self.assertEqual(model.calls, ["discounts"])
                self.assertEqual(observed, {})
                self.assertNotIn(result["status"], ("validated", "conditioned_draft"))


if __name__ == "__main__":
    unittest.main()
