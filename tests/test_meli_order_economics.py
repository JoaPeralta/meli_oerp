"""Synthetic arithmetic fixtures, not assertions about undocumented API responses."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("economics", ROOT / "models/meli_order_economics.py")
economics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(economics)


def observation(body, status=200, missing=None):
    return {"body": body, "status": status, "missing": missing}


def order():
    return {"id": 123, "currency_id": "ARS", "status": "paid", "seller": {"id": 7},
            "total_amount": 200, "paid_amount": 200, "shipping": None,
            "order_items": [{"item": {"id": "MLA1"}, "quantity": 2, "unit_price": 100,
                             "currency_id": "ARS", "gross_price": 200}],
            "payments": [{"id": 77, "status": "approved", "currency_id": "ARS",
                          "transaction_amount": 200, "total_paid_amount": 200}]}


def benefit(kind="discount", total=20, seller=10, quantity=2):
    ids = {"discount": {"supplier": {"offer_id": "offer1", "funding_mode": "sale_fee"}},
           "coupon": {"coupon": {"id": 3}, "supplier": {"meli_campaign": "campaign1"}},
           "cashback": {"cashback": {"id": 4}, "supplier": {"campaign_id": "campaign2"}}}
    return dict(type=kind, items=[{"id": "MLA1", "quantity": quantity,
                                 "amounts": {"total": total, "seller": seller}}], **ids[kind])


class OrderEconomicsTests(unittest.TestCase):
    def test_review_nested_campaign_metadata_never_reaches_snapshot(self):
        b = benefit("cashback", 5, 0)
        b["supplier"]["campaign_id"] = {"access_token": "SYNTHETIC_SECRET",
                                            "payer": {"email": "private@example.test"}}
        result = self.interpret(details=[b])
        self.assertEqual(result["reason"], "economic_metadata_invalid")
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        self.assertNotIn("private@example.test", json.dumps(result))

    def test_review_explicit_coupon_identity_must_match_discount_detail(self):
        p = order()
        p["paid_amount"] = p["payments"][0]["total_paid_amount"] = 180
        p["coupon"] = {"id": "COUPON_A", "amount": 20}
        b = benefit("coupon")
        b["coupon"]["id"] = "COUPON_B"
        result = self.interpret(p, [b])
        self.assertEqual((result["status"], result["reason"]),
                         ("contradictory", "coupon_identity_mismatch"))

    def test_all_audited_metadata_rejects_non_scalar_unbounded_values(self):
        bad_values = ({"secret": "SYNTHETIC_SECRET"}, ["SYNTHETIC_SECRET"],
                      True, float("nan"), "x" * 129, "bad\nidentifier", 10 ** 130)
        for field in ("campaign_id", "meli_campaign", "offer_id", "funding_mode", "element_id"):
            for bad in bad_values:
                b = benefit("cashback", 5, 0)
                target = b["items"][0] if field == "element_id" else b["supplier"]
                target[field] = bad
                result = self.interpret(details=[b])
                with self.subTest(field=field, bad_type=type(bad).__name__):
                    self.assertEqual(result["reason"], "economic_metadata_invalid")
                    self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
                    self.assertEqual(result["benefits"], [])

    def test_required_and_optional_identifier_types_are_bounded(self):
        for field in ("benefit", "item", "payment", "currency"):
            for bad in ({"secret": "SYNTHETIC_SECRET"}, [], False, float("nan"), "x" * 129):
                p, b = order(), benefit("cashback", 5, 0)
                if field == "benefit":
                    b["cashback"]["id"] = bad
                elif field == "item":
                    p["order_items"][0]["item"]["id"] = bad
                elif field == "payment":
                    p["payments"][0]["id"] = bad
                else:
                    p["currency_id"] = bad
                result = self.interpret(p, [b])
                with self.subTest(field=field, bad_type=type(bad).__name__):
                    self.assertNotEqual(result["status"], "validated")
                    self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

    def test_scalar_metadata_preserves_ids_and_does_not_invent_mode_enums(self):
        b = benefit("cashback", 5, 0)
        b["supplier"].update(campaign_id=123, meli_campaign="123", offer_id="MLA1-abc", funding_mode="future_documented_mode")
        b["items"][0]["element_id"] = 1
        result = self.interpret(details=[b])
        self.assertEqual(result["status"], "validated", result)
        stored = result["benefits"][0]
        self.assertEqual((stored["campaign_id"], stored["offer_id"], stored["element_id"]),
                         ("123", "MLA1-abc", "1"))
        self.assertEqual(stored["funding_mode"], "future_documented_mode")
        b["supplier"]["campaign_id"] = "different"
        result = self.interpret(details=[b])
        self.assertEqual((result["status"], result["reason"]),
                         ("contradictory", "benefit_campaign_identity_mismatch"))

    def test_coupon_identity_normalizes_numeric_string_but_never_selects_one_of_many(self):
        p = order()
        p["paid_amount"] = p["payments"][0]["total_paid_amount"] = 180
        p["coupon"] = {"id": 3, "amount": 20}
        b = benefit("coupon")
        b["coupon"]["id"] = "3"
        self.assertEqual(self.interpret(p, [b])["status"], "validated")
        b["items"][0]["amounts"] = {"total": 10, "seller": 0}
        second = copy.deepcopy(b)
        second["coupon"]["id"] = 4
        result = self.interpret(p, [b, second])
        self.assertEqual((result["status"], result["reason"]),
                         ("pending", "coupon_identity_ambiguous"))
        p["coupon"] = {"amount": 20}
        result = self.interpret(p, [b, second])
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual({row["id"] for row in result["benefits"]}, {"3", "4"})
        self.assertIsNone(result["order_coupon_id"])
        self.assertEqual(result["order_coupon_id_state"], "absent")

    def test_coupon_explicit_unknown_or_bad_identity_is_not_zero_or_assumed_match(self):
        p = order()
        p["paid_amount"] = p["payments"][0]["total_paid_amount"] = 180
        p["coupon"] = {"id": None, "amount": 20}
        result = self.interpret(p, [benefit("coupon")])
        self.assertEqual(result["status"], "validated")
        self.assertIsNone(result["order_coupon_id"])
        self.assertEqual(result["order_coupon_id_state"], "unknown")
        p["coupon"]["id"] = {"secret": "SYNTHETIC_SECRET"}
        result = self.interpret(p, [benefit("coupon")])
        self.assertEqual(result["reason"], "economic_metadata_invalid")
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        p["coupon"]["id"] = 0
        result = self.interpret(p, [benefit("coupon")])
        self.assertEqual(result["reason"], "coupon_identity_mismatch")


    def interpret(self, payload=None, details=None, **kwargs):
        return economics.interpret_order_economics(payload or order(),
            observation({"details": [] if details is None else details}), **kwargs)

    def test_empty_200_validates_arithmetic_without_claiming_universal_absence(self):
        result = self.interpret()
        self.assertEqual((result["status"], result["commercial_total"], result["buyer_paid"]),
                         ("validated", "200", "200"))
        self.assertEqual(result["benefits"], [])
        self.assertEqual(result["invoice_product_basis"], "200")
        json.dumps(result, allow_nan=False)

    def test_exact_404_is_specific_but_never_fabricates_absence(self):
        for p in (order(), dict(order(), coupon={"amount": 0})):
            result = economics.interpret_order_economics(p,
                observation({"error": "discount_not_found"}, 404))
            self.assertEqual((result["status"], result["reason"]),
                             ("pending", "discount_not_found_evidence_incomplete"))
            self.assertIsNone(result["seller_funded"])

    def test_http_and_partial_observations_stay_strict(self):
        for status in (401, 403, 404, 500, 503):
            result = economics.interpret_order_economics(order(), observation({"error": "other"}, status))
            self.assertEqual(result["reason"], "discounts_http_%s" % status)
            self.assertNotEqual(result["status"], "validated")
        for response in (observation({}, 200), observation({"details": []}, 200, "true"),
                         observation({"details": [], "error": "bad"})):
            self.assertNotEqual(economics.interpret_order_economics(order(), response)["status"], "validated")

    def test_price_and_quantity_discount_is_already_in_effective_price(self):
        p = order()
        p["order_items"][0].update(gross_price=220, discounts=[{"amounts": {"full": 10, "seller": 5}}])
        result = self.interpret(p, [benefit()])
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(result["items"][0]["unit_price"], "100")
        self.assertEqual(result["items"][0]["reference_total"], "220")
        self.assertTrue(result["benefits"][0]["incorporated_in_unit_price"])
        self.assertEqual(result["benefits"][0]["quantity"], 2)
        self.assertEqual((result["seller_funded"], result["marketplace_funded"]), ("10", "10"))

    def test_coupon_funding_does_not_reduce_invoice_basis(self):
        for seller in (0, 20, 7):
            p = order()
            p["paid_amount"] = p["payments"][0]["total_paid_amount"] = 180
            p["coupon"] = {"id": 3, "amount": 20}
            result = self.interpret(p, [benefit("coupon", seller=seller)])
            self.assertEqual(result["status"], "validated", result)
            self.assertEqual(result["invoice_product_basis"], "200")
            self.assertEqual(result["benefits"][0]["effect"], "buyer_payment_benefit")
            self.assertEqual(result["seller_funded"], str(seller))

    def test_cashback_does_not_change_price_or_buyer_payment(self):
        result = self.interpret(details=[benefit("cashback", 5, 0)])
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(result["invoice_product_basis"], "200")
        self.assertEqual(result["benefits"][0]["effect"], "cashback")
        self.assertFalse(result["benefits"][0]["incorporated_in_unit_price"])

    def test_combined_benefits_and_exact_duplicate_are_not_applied_twice(self):
        p = order()
        p["paid_amount"] = p["payments"][0]["total_paid_amount"] = 180
        p["order_items"][0]["gross_price"] = 220
        d = benefit()
        result = self.interpret(p, [d, copy.deepcopy(d), benefit("coupon"), benefit("cashback", 5, 0)])
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(len(result["benefits"]), 3)
        self.assertEqual(result["commercial_total"], "200")

    def test_conflicting_duplicate_is_rejected(self):
        first = benefit()
        second = copy.deepcopy(first)
        second["items"][0]["amounts"]["seller"] = 3
        self.assertEqual(self.interpret(details=[first, second])["status"], "contradictory")

    def test_missing_seller_is_unknown_not_zero(self):
        b = benefit()
        del b["items"][0]["amounts"]["seller"]
        result = self.interpret(details=[b])
        self.assertEqual(result["reason"], "benefit_funding_missing")
        self.assertIsNone(result["seller_funded"])

    def test_missing_reference_does_not_invent_historical_list_price(self):
        p = order()
        del p["order_items"][0]["gross_price"]
        p["order_items"][0]["full_unit_price"] = 999
        result = self.interpret(p)
        self.assertEqual(result["status"], "validated")
        self.assertIsNone(result["items"][0]["reference_total"])
        self.assertEqual(result["items"][0]["unverified_full_unit_price"], "999")

    def test_multiple_products_are_attributed_without_proration(self):
        p = order()
        p["order_items"].append({"item": {"id": "MLA2"}, "quantity": 3, "unit_price": 10})
        p["total_amount"] = p["paid_amount"] = 230
        p["payments"][0].update(transaction_amount=230, total_paid_amount=230)
        result = self.interpret(p, [benefit("cashback", 5, 0)])
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(result["benefits"][0]["item_id"], "MLA1")
        b = benefit(); b["items"][0]["id"] = "MLAX"
        self.assertEqual(self.interpret(p, [b])["reason"], "benefit_item_unbound")

    def test_unknown_variant_allocation_or_type_is_pending(self):
        p = order()
        p["order_items"].append(copy.deepcopy(p["order_items"][0]))
        p["total_amount"] = p["paid_amount"] = 400
        p["payments"][0].update(transaction_amount=400, total_paid_amount=400)
        self.assertEqual(self.interpret(p, [benefit()])["reason"], "item_identity_ambiguous")
        b = benefit(); b["type"] = "future_promotion"
        self.assertEqual(self.interpret(details=[b])["status"], "unsupported")

    def test_bad_numeric_or_contradictory_totals_never_validate(self):
        for bad in (True, None, "NaN", "Infinity", -1):
            p = order(); p["order_items"][0]["unit_price"] = bad
            self.assertNotEqual(self.interpret(p)["status"], "validated")
        p = order(); p["total_amount"] = 199
        self.assertEqual(self.interpret(p)["reason"], "commercial_total_mismatch")
        p = order(); p["order_items"][0]["gross_price"] = 199
        self.assertEqual(self.interpret(p)["reason"], "reference_price_contradiction")

    def test_separate_shipping_is_not_assumed_part_of_paid_amount(self):
        p = order()
        mp = observation({"id": 77, "status": "approved", "currency_id": "ARS",
                          "transaction_amount": 200, "shipping_amount": 10,
                          "transaction_details": {"total_paid_amount": 200}, "coupon_amount": 0})
        result = self.interpret(p, payment_observations={"77": mp})
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(result["shipping_amount"], "10")
        self.assertEqual(result["shipping_in_buyer_paid"], False)
        self.assertEqual(result["invoice_product_basis"], "200")

    def test_mp_is_corroboration_not_funding_source_and_is_sanitized(self):
        p = order()
        mp = observation({"id": 77, "status": "approved", "currency_id": "ARS",
                          "transaction_amount": 200, "shipping_amount": 0,
                          "transaction_details": {"total_paid_amount": 200, "net_received_amount": 170},
                          "coupon_amount": 0, "payer": {"email": "private@example.test"},
                          "charges_details": [{"type": "coupon", "amounts": {"original": 100}}]})
        result = self.interpret(p, payment_observations={"77": mp})
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(result["seller_funded"], "0")
        self.assertNotIn("private@example.test", json.dumps(result))
        self.assertEqual(result["payments"][0]["net_received"], "170")
        mp["body"]["transaction_amount"] = 199
        self.assertEqual(self.interpret(p, payment_observations={"77": mp})["reason"], "payment_transaction_mismatch")

    def test_unexplained_paid_difference_or_incomplete_payments_pending(self):
        p = order(); p["paid_amount"] = p["payments"][0]["total_paid_amount"] = 199
        self.assertEqual(self.interpret(p)["reason"], "buyer_payment_difference_unexplained")
        p = order(); p["payments"] = []
        self.assertEqual(self.interpret(p)["reason"], "payments_missing")

    def test_rounded_input_totals_are_not_silently_balanced(self):
        p = order()
        p["order_items"][0].update(unit_price="33.333", quantity=3, gross_price="99.999")
        p["total_amount"] = p["paid_amount"] = 100
        p["payments"][0].update(transaction_amount=100, total_paid_amount=100)
        self.assertEqual(self.interpret(p)["reason"], "commercial_total_mismatch")

    def test_mp_collector_if_reported_must_match_admitted_order_seller(self):
        mp = observation({"id": 77, "status": "approved", "currency_id": "ARS", "collector_id": 8,
                          "transaction_amount": 200, "transaction_details": {"total_paid_amount": 200}})
        self.assertEqual(self.interpret(payment_observations={"77": mp})["reason"], "payment_collector_mismatch")
        mp["body"]["collector_id"] = 7
        self.assertEqual(self.interpret(payment_observations={"77": mp})["status"], "validated")

    def test_interpreter_is_pure_deterministic_and_reimport_result_identical(self):
        p, d = order(), [benefit("cashback", 5, 0)]
        before = copy.deepcopy((p, d))
        first = self.interpret(p, d)
        self.assertEqual(first, self.interpret(p, d))
        self.assertEqual((p, d), before)


if __name__ == "__main__":
    unittest.main()
