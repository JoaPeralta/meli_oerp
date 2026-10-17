"""Execute existing public entry and the bounded mixed-pack validation contract.

Source doubles expose the real baseline barrier; native tests own ORM proof.
"""
import ast
import copy
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest

from test_meli_t4_pack_commerce import admission, method, payload
from test_meli_t4_cancellation import kernel


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT.parent / "odoo-t4-c-05-mixed-updates/addons/viarengo_circuit_meli/models/mixed_updates.py"


class Rows(list):
    def mapped(self, name):
        return [getattr(row, name) for row in self]

    def __getattr__(self, name):
        if len(self) == 1:
            return getattr(self[0], name)
        raise AttributeError(name)


class MixedSnapshotTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(BRIDGE.read_text())
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "mixed_snapshot")
        scope = {"deepcopy": copy.deepcopy, "AdmissionError": admission.AdmissionError,
                 "remote_id": admission.remote_id, "item_key": admission.item_key,
                 "verify_order": admission.verify_order, "amount": kernel.amount,
                 "cancellation_evidence": kernel.cancellation_evidence,
                 "reconciliation_reason": kernel.reconciliation_reason}
        exec(compile(ast.Module([node], []), str(BRIDGE), "exec"), scope)
        self.validate = scope["mixed_snapshot"]
        self.pack = {"id": 900, "status": "released", "status_detail": None,
                     "buyer": {"id": 50}, "shipment": None, "orders": [{"id": 101}, {"id": 102}]}

    def cancelled(self, **changes):
        body = payload()
        body.update(status="cancelled", cancel_detail={"code": "buyer_cancelled"})
        body.update(changes)
        return body

    def test_original_cancelled_snapshot_is_not_rewritten_paid(self):
        body = self.cancelled()
        before = copy.deepcopy(body)
        self.assertEqual(self.validate(self.pack, body, "42"), before)
        self.assertEqual(body, before)

    def test_positive_paid_quantity_price_update_is_supported(self):
        result = self.validate(self.pack, payload(102, 4, 125), "42")
        self.assertEqual((result["total_amount"], result["order_items"][0]["quantity"]), (500, 4))

    def test_cancelled_reported_paid_is_not_forced_to_original_total(self):
        self.assertEqual(self.validate(self.pack, self.cancelled(paid_amount=100), "42")["paid_amount"], 100)

    def test_pending_partial_refunded_and_resurrected_nonpaid_states_refuse(self):
        for status in ("pending_cancel", "partially_refunded", "invalid", "confirmed"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.validate(self.pack, self.cancelled(status=status), "42")

    def test_wrong_seller_pack_buyer_currency_and_optional_shapes_refuse(self):
        for changes in ({"seller": {"id": 43}}, {"pack_id": 901}, {"buyer": {"id": 51}},
                        {"currency_id": "ARS"}, {"shipping": []}, {"coupon": []}, {"tags": {}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate(self.pack, self.cancelled(**changes), "42")

    def test_split_kit_delivered_and_shared_costs_refuse(self):
        for changes in ({"cancel_detail": {"code": "pack_splitted"}}, {"tags": ["kit"]},
                {"tags": ["delivered"]}, {"shipping_cost": 1}, {"coupon": {"amount": 1}},
                {"financing_fee_amount": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate(self.pack, self.cancelled(**changes), "42")

    def test_unknown_quantity_price_and_fee_or_paid_allocation_refuse(self):
        for field, value in (("quantity", 0), ("quantity", True), ("unit_price", float("nan")),
                             ("sale_fee", 1), ("full_unit_price", 99)):
            body = payload(102, 4, 125)
            body["order_items"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.validate(self.pack, body, "42")
        with self.assertRaises(ValueError):
            self.validate(self.pack, dict(payload(102, 4, 125), paid_amount=499), "42")

    def test_explicit_matching_shipment_does_not_project_logistics(self):
        self.pack["shipment"] = {"id": 800}
        body = self.cancelled(shipping={"id": 800})
        self.assertEqual(self.validate(self.pack, body, "42")["shipping"], {"id": 800})
        with self.assertRaises(ValueError):
            self.validate(self.pack, self.cancelled(), "42")


class PublicMixedEntryTests(unittest.TestCase):
    def receiver(self, late=None):
        original = payload(101, 2, 100)
        cancelled = copy.deepcopy(original)
        cancelled.update(status="cancelled", cancel_detail={"code": "buyer_cancelled"})
        paid = payload(102, 4, 125)
        company = SimpleNamespace(id=1, _fields={})
        root = SimpleNamespace(meli_orders=Rows([SimpleNamespace(status="cancelled"),
                                               SimpleNamespace(status="paid")]))
        current = SimpleNamespace(order_id="102", sale_order=root)
        local = Rows([SimpleNamespace(order_id="101"), SimpleNamespace(order_id="102")])
        pack = {"id": 900, "status": "released", "status_detail": None, "buyer": {"id": 50},
                "shipment": None, "orders": [{"id": 101}, {"id": 102}]}
        class Client:
            access_token = None
            status_code = last_status_code = 200
            def get(self, path, params):
                self.body = copy.deepcopy({"/packs/900": pack, "/orders/101": cancelled,
                    "/orders/102": paid, "/orders/101/discounts": {"details": []},
                    "/orders/102/discounts": {"details": []}}[path])
                return self
            def json(self):
                return self.body
        class ExistingParent:
            pass
        ExistingParent._meli_import_pack = method("_meli_import_pack")
        ExistingParent._meli_collect_pack_commerce = method("_meli_collect_pack_commerce")
        selected = ExistingParent
        if BRIDGE.exists():
            tree = ast.parse(BRIDGE.read_text())
            cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "MixedPackUpdates")
            cls.bases = [ast.Name(id="ExistingParent", ctx=ast.Load())]
            cls.decorator_list = []
            for node in cls.body:
                if isinstance(node, ast.FunctionDef):
                    node.decorator_list = []
            scope = {"ExistingParent": ExistingParent, "remote_id": admission.remote_id,
                     "AdmissionError": admission.AdmissionError, "ValidationError": ValueError}
            exec(compile(ast.fix_missing_locations(ast.Module([cls], [])), str(BRIDGE), "exec"), scope)
            selected = scope["MixedPackUpdates"]
        receiver = selected()
        receiver.env = SimpleNamespace(context={}, user=SimpleNamespace(company_id=company),
                                       cr=SimpleNamespace(savepoint=nullcontext))
        receiver._meli_assert_order_admission_actor = lambda config: "42"
        receiver._meli_preflight_order_history = lambda *args, **kwargs: None
        receiver._meli_pack_local_orders = lambda *args: local
        receiver._meli_order_admission = lambda *args: {"complete": True, "orders": [paid], "responses": []}
        receiver.search = lambda *args: Rows([current])
        calls = []
        def update(*args):
            calls.append(args)
            if late:
                raise late
            return {"mixed_pack_update": "applied", "active_total": 500}
        current._meli_update_mixed_paid = update
        return receiver, company, Client(), paid, calls

    def test_actual_public_paid_entry_routes_existing_audited_mixed_pack(self):
        receiver, company, client, body, calls = self.receiver()
        try:
            result = method("orders_update_order_json", prefix=True)(receiver,
                {"id": 2, "order_json": body}, config=company, meli=client)
        except (ValueError, admission.AdmissionError) as exc:
            self.fail("EXISTING_MIXED_PACK_UPDATE_BLOCKED: " + str(exc))
        self.assertEqual(result["active_total"], 500)
        self.assertEqual(len(calls), 1)

    def test_public_entry_preserves_original_late_error_object_and_marker(self):
        failure = RuntimeError("synthetic late mixed update failure")
        receiver, company, client, body, calls = self.receiver(failure)
        try:
            method("orders_update_order_json", prefix=True)(receiver,
                {"id": 2, "order_json": body}, config=company, meli=client)
        except RuntimeError as exc:
            self.assertIs(exc, failure)
            self.assertTrue(exc._meli_pack_operation_failure)
        except (ValueError, admission.AdmissionError) as exc:
            self.fail("MIXED_ENTRY_NEVER_REACHED_LATE_FAILURE: " + str(exc))
        else:
            self.fail("The late error must escape")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
