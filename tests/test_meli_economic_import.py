"""Execute actual acquisition/preflight and importer prefix with synthetic doubles."""
import ast
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

try:
    from .test_meli_order_economics import economics, observation, order
except ImportError:
    from test_meli_order_economics import economics, observation, order

ROOT = Path(__file__).resolve().parents[1]
admission_spec = importlib.util.spec_from_file_location("admission", ROOT / "models/meli_pack_admission.py")
admission = importlib.util.module_from_spec(admission_spec)
admission_spec.loader.exec_module(admission)


def methods():
    tree = ast.parse((ROOT / "models/orders.py").read_bytes())
    return {n.name: copy.deepcopy(n) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def operation(name, namespace=None):
    node = methods()[name]
    node.decorator_list = []
    scope = dict(interpret_order_economics=economics.interpret_order_economics,
                 ValidationError=ValueError, remote_id=admission.remote_id,
                 AdmissionError=admission.AdmissionError, **(namespace or {}))
    exec(compile(ast.fix_missing_locations(ast.Module([node], [])), "orders.py", "exec"), scope)
    return scope[name]


class EconomicImportTests(unittest.TestCase):
    def test_review_invalid_payment_id_never_strips_mp_corroboration(self):
        payload = order()
        payload["payments"][0]["id"] = "not-a-payment"
        payload = admission.verify_order(payload, "7")
        model = self.producer()
        result, payments = model._meli_economic_preflight(payload, object())
        self.assertEqual(result["reason"], "payment_identity_invalid")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(payments, {})
        self.assertEqual(model.calls, ["discounts"])

    def test_invalid_payment_population_never_falls_back_or_fetches_a_prefix(self):
        bad_ids = (None, "not-a-payment", "0", -1, False, {}, [], "9" * 129)
        for bad in bad_ids:
            p = order()
            p["payments"].append(dict(p["payments"][0], id=bad))
            model = self.producer()
            result, payments = model._meli_economic_preflight(p, object())
            with self.subTest(bad_type=type(bad).__name__):
                self.assertEqual(result["reason"], "payment_identity_invalid")
                self.assertEqual(payments, {})
                self.assertEqual(model.calls, ["discounts"])
        for population in (None, [], {}, "payments", [None], [{}]):
            p = order()
            p["payments"] = population
            model = self.producer()
            result, payments = model._meli_economic_preflight(p, object())
            with self.subTest(population_type=type(population).__name__):
                self.assertNotEqual(result["status"], "validated")
                self.assertEqual(model.calls, ["discounts"])
                self.assertEqual(payments, {})

    def test_failed_payment_acquisition_cannot_be_replaced_by_order_only_evidence(self):
        for response, reason in ((observation({}, 401), "payment_http_401"),
                                 (observation({}, 403), "payment_http_403"),
                                 (observation({}, 500), "payment_http_500"),
                                 (observation(None, None), "payment_http_unknown"),
                                 (observation({}, 200, "true"), "payment_observation_incomplete")):
            model = self.producer(payment=response)
            result, payments = model._meli_economic_preflight(order(), object())
            with self.subTest(reason=reason):
                self.assertEqual(result["reason"], reason)
                self.assertNotEqual(result["status"], "validated")
                self.assertEqual(set(payments), {"77"})
                self.assertEqual(model.calls, ["discounts", ("payment", "77")])

    def test_complete_multiple_payment_population_is_preserved(self):
        p = order()
        p["order_items"][0].update(quantity=4, gross_price=400)
        p["total_amount"] = p["paid_amount"] = 400
        p["payments"].append(dict(p["payments"][0], id=88))
        model = self.producer()
        result, payments = model._meli_economic_preflight(p, object())
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(set(payments), {"77", "88"})
        self.assertEqual(model.calls, ["discounts", ("payment", "77"), ("payment", "88")])
        p["payments"][1]["id"] = "077"
        model = self.producer()
        result, payments = model._meli_economic_preflight(p, object())
        self.assertEqual(result["reason"], "payment_identity_ambiguous")
        self.assertEqual(model.calls, ["discounts"])

    def producer(self, discounts=None, payment=None):
        model = SimpleNamespace(calls=[])
        model._fetch_order_discounts = lambda **kw: model.calls.append("discounts") or (
            discounts if discounts is not None else observation({"details": []}))
        model._meli_fetch_payment_observation = lambda pid, meli: model.calls.append(("payment", str(pid))) or (
            payment if payment is not None else observation({"id": int(pid), "status": "approved", "currency_id": "ARS",
                "transaction_amount": 200, "shipping_amount": 0, "coupon_amount": 0,
                "transaction_details": {"total_paid_amount": 200}}))
        model._meli_economic_preflight = lambda payload, meli: operation("_meli_economic_preflight")(model, payload, meli)
        return model

    def test_preflight_reuses_exact_mp_observation_and_pure_interpreter(self):
        model = self.producer()
        result, payments = model._meli_economic_preflight(order(), object())
        self.assertEqual(result["status"], "validated", result)
        self.assertEqual(model.calls, ["discounts", ("payment", "77")])
        self.assertEqual(result, economics.interpret_order_economics(order(), observation({"details": []}), payments))

    def test_transport_failure_has_no_estimate_or_payment_request(self):
        for status, body in ((404, {"error": "other_not_found"}), (401, {}), (500, {})):
            model = self.producer(observation(body, status))
            result, payments = model._meli_economic_preflight(order(), object())
            self.assertEqual(result["status"], "pending")
            self.assertEqual(model.calls, ["discounts"])
            self.assertEqual(payments, {})

    def test_actual_importer_returns_before_any_commercial_model_is_reached(self):
        node = methods()["orders_update_order_json"]
        node.decorator_list = []
        stop = next(i for i, stmt in enumerate(node.body) if isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "saleorder_obj" for t in stmt.targets))
        node.body = node.body[:stop] + ast.parse("raise AssertionError('commercial mutation reached')").body
        model = self.producer(observation({"error": "discount_not_found"}, 404),
                              payment=observation({}, 503))
        model.env = SimpleNamespace(context={}, user=SimpleNamespace(company_id=SimpleNamespace(id=1)))
        model._meli_assert_order_admission_actor = lambda config: "7"
        model._meli_preflight_order_history = lambda *a, **kw: None
        model._meli_order_admission = lambda *a: {"pending": None}
        config = SimpleNamespace(_fields={}, id=1)
        scope = {"verify_order": lambda body, *a, **kw: body, "AdmissionError": ValueError,
                 "ValidationError": ValueError}
        exec(compile(ast.fix_missing_locations(ast.Module([node], [])), "actual-import-prefix", "exec"), scope)
        result = scope[node.name](model, {"id": False, "order_json": order()}, config=config, meli=object())
        self.assertEqual(result["economic_pending"], "payment_http_503")
        self.assertNotIn("commercial mutation reached", str(result))

    def test_snapshot_upsert_overwrites_identically_without_duplicate_records(self):
        result = economics.interpret_order_economics(order(), observation({"details": []}))
        record = SimpleNamespace(writes=[], ensure_one=lambda: None, order_id="123", currency_id="ARS")
        record.write = lambda values: record.writes.append(copy.deepcopy(values)) or True
        apply = operation("_meli_store_economic_snapshot")
        apply(record, result)
        apply(record, copy.deepcopy(result))
        self.assertEqual(record.writes, [{"economic_snapshot": result}] * 2)
        invalid = dict(result, status="pending")
        with self.assertRaises(ValueError):
            apply(record, invalid)
        self.assertEqual(len(record.writes), 2)

    def test_unsafe_estimator_and_coupon_proration_are_absent(self):
        source = (ROOT / "models/orders.py").read_text(encoding="utf8")
        self.assertTrue("def _estimate_seller_discount_from_charges" not in source)
        importer = ast.unparse(methods()["orders_update_order_json"])
        self.assertNotIn("discount_pct", importer)
        self.assertNotIn("coupon_fee_amount", importer)

    def test_invoice_basis_is_commercial_not_paid_minus_seller_funding(self):
        result = economics.interpret_order_economics(order(), observation({"details": []}))
        source = SimpleNamespace(economic_snapshot=result, currency_id="ARS", total_amount=200)
        model = SimpleNamespace(meli_orders=[source], meli_currency_id="ARS", meli_total_amount=200,
            meli_shipping_id=False, amount_total=200, meli_paid_amount=180, meli_discount_seller_amount=20)
        config = SimpleNamespace(_fields={"mercadolibre_order_total_config": True,
            "mercadolibre_including_shipping_cost": True}, mercadolibre_order_total_config="paid_amount",
            mercadolibre_including_shipping_cost="never")
        method = operation("meli_amount_to_invoice", {"money": economics.money})
        self.assertEqual(method(model, config=config), 200)
        config.mercadolibre_order_total_config = "manual"
        self.assertEqual(method(model, config=config), 0)
        source.economic_snapshot = None
        config.mercadolibre_order_total_config = "paid_amount"
        self.assertEqual(method(model, config=config), 0)


if __name__ == "__main__":
    unittest.main()
