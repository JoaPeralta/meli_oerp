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
        for status, body in ((404, {"error": "discount_not_found"}), (401, {}), (500, {})):
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
        model = self.producer(observation({"error": "discount_not_found"}, 404))
        model.env = SimpleNamespace(context={}, user=SimpleNamespace(company_id=SimpleNamespace(id=1)))
        model._meli_assert_order_admission_actor = lambda config: "7"
        model._meli_preflight_order_history = lambda *a, **kw: None
        model._meli_order_admission = lambda *a: {"pending": None}
        config = SimpleNamespace(_fields={}, id=1)
        scope = {"verify_order": lambda body, *a, **kw: body, "AdmissionError": ValueError,
                 "ValidationError": ValueError}
        exec(compile(ast.fix_missing_locations(ast.Module([node], [])), "actual-import-prefix", "exec"), scope)
        result = scope[node.name](model, {"id": False, "order_json": order()}, config=config, meli=object())
        self.assertEqual(result["economic_pending"], "discount_not_found_evidence_incomplete")
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
