"""Source-level cancellation entry and pure-read regressions; not ORM proof."""
import copy
import ast
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace, MethodType
from types import ModuleType
import unittest

from test_meli_t4_pack_commerce import method, payload


ROOT = Path(__file__).resolve().parents[1]


def bridge_source(name):
    """An explicit paired checkout, never a historical worktree name guess."""
    root = os.environ.get("VIARENGO_ROOT")
    if not root:
        raise RuntimeError("VIARENGO_ROOT must select the paired Odoo checkout")
    return Path(root).resolve() / "addons/viarengo_circuit_meli/models" / name
package = ModuleType("c5_contract")
package.__path__ = [str(ROOT / "models")]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("c5_contract.meli_cancellation", ROOT / "models/meli_cancellation.py")
kernel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kernel)


class CancellationEntryTests(unittest.TestCase):
    def test_confirmed_child_cancellation_reaches_explicit_private_route(self):
        document = payload()
        document.update(status="cancelled", cancel_detail={"code": "buyer_cancelled"})
        company = SimpleNamespace(id=1, _fields={})
        probe = SimpleNamespace(env=SimpleNamespace(context={}, user=SimpleNamespace(company_id=company)),
            _meli_assert_order_admission_actor=lambda config: "42",
            _meli_apply_cancellation=lambda *args, **kwargs: {"cancellation": "applied"})
        try:
            result = method("orders_update_order_json", prefix=True)(probe,
                {"id": 1, "remote_order_id": "101", "order_json": document}, config=company, meli=object())
        except ValueError as exc:
            self.fail("CONFIRMED_CANCEL_REMAINS_BLOCKED: " + str(exc))
        self.assertEqual(result, {"cancellation": "applied"}, "CONFIRMED_CANCEL_REMAINS_BLOCKED")

    def test_status_read_does_not_overwrite_remote_root_fields(self):
        child = SimpleNamespace(status="cancelled", status_detail="buyer cancelled",
                                shipment_status=False, shipment_substatus=False, pack_id="900")
        root = SimpleNamespace(meli_orders=[child], meli_status="paid",
                               meli_status_detail="remote root evidence", meli_order_id="pack:900")
        before = copy.deepcopy((root.meli_status, root.meli_status_detail))
        receiver = [root]
        class Roots(list):
            MELI_STATUS_LABELS = {}
        method("_meli_status_brief")(Roots(receiver))
        self.assertEqual((root.meli_status, root.meli_status_detail), before,
                         "STATUS_READ_OVERWROTE_REMOTE_EVIDENCE")


class CancellationEvidenceTests(unittest.TestCase):
    def document(self, **values):
        body = payload()
        body.update(status="cancelled", cancel_detail={"code": "buyer_cancelled", "date": "2026-10-17"})
        body.update(values)
        return body

    def test_paid_amount_is_reported_not_automatically_zero(self):
        body = self.document()
        before = copy.deepcopy(body)
        result = kernel.cancellation_evidence(body, "42", "101", "900")
        self.assertEqual(result["paid_amount"], "200")
        self.assertEqual(result["cancel_detail"], body["cancel_detail"])
        self.assertEqual(body, before)

    def test_wrong_seller_order_pack_and_missing_currency_refuse(self):
        for change in ({"seller": {"id": 43}}, {"id": 102}, {"pack_id": 901}, {"currency_id": None}):
            with self.subTest(change=change), self.assertRaises(kernel.AdmissionError):
                kernel.cancellation_evidence(self.document(**change), "42", "101", "900")

    def test_nonterminal_states_are_observations_not_final_cancellations(self):
        for status in ("pending_cancel", "partially_refunded"):
            body = self.document(status=status)
            self.assertEqual(kernel.cancellation_evidence(body, "42")["status"], status)
            self.assertEqual(kernel.reconciliation_reason(body), "remote_cancellation_not_terminal")

    def test_split_and_transformation_are_not_ordinary_cancellations(self):
        for code in ("pack_splitted", "bundle_component", "kit_transformed"):
            self.assertEqual(kernel.reconciliation_reason(self.document(cancel_detail={"code": code})),
                             "split_or_transformation_requires_reconciliation")

    def test_shared_amounts_refuse_without_allocation(self):
        for change in ({"coupon": {"amount": 2}}, {"shipping": {"cost": 5}},
                       {"financing_fee_amount": 1}, {"discount_seller_amount": 1}):
            with self.subTest(change=change):
                self.assertEqual(kernel.reconciliation_reason(self.document(**change)),
                                 "shared_allocation_requires_reconciliation")

    def test_zero_is_explicit_but_absence_is_not_zero(self):
        body = self.document(paid_amount=0)
        self.assertEqual(kernel.cancellation_evidence(body, "42")["paid_amount"], "0")
        del body["paid_amount"]
        self.assertNotIn("paid_amount", kernel.cancellation_evidence(body, "42"))
        for invalid in (None, True, -1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(kernel.AdmissionError):
                kernel.cancellation_evidence(self.document(paid_amount=invalid), "42")

    def test_audit_whitelist_omits_payment_tokens_and_raw_body(self):
        body = self.document(payments=[{"id": 7, "status": "approved", "url": "token-bearing",
                                       "access_token": "never", "full_payment": {"private": "never"}}])
        text = json.dumps(kernel.cancellation_evidence(body, "42"))
        self.assertNotIn("never", text)
        self.assertNotIn("token-bearing", text)


class FinancialEvidenceTests(unittest.TestCase):
    def row(self, **body):
        captured = {"id": 7, "status": "approved", "status_detail": "accredited", "currency_id": "USD",
                    "date_last_updated": "2026-10-17T00:00:00Z", "transaction_details": {"total_paid_amount": 200}}
        captured.update(body)
        return {"payment_id": "7", "status": captured["status"], "currency_id": "USD", "full_payment": captured}

    def test_approved_without_explicit_accredited_is_unknown(self):
        row = self.row()
        del row["full_payment"]["status_detail"]
        self.assertEqual(kernel.payment_summary([row], "USD")[0]["credited"], "Unknown")

    def test_missing_snapshot_default_floats_are_not_refund_proof(self):
        result = kernel.payment_summary([{"payment_id": "7", "status": "refunded", "currency_id": "USD",
                                          "full_payment": False, "total_paid_amount": 0}], "USD")[0]
        self.assertEqual((result["credited"], result["confirmed_refund"]), ("Unknown", "Unknown"))

    def test_identical_shared_payment_is_one_identity_not_a_double_total(self):
        row = self.row()
        result = kernel.payment_summary([row, copy.deepcopy(row)], "USD")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["credited"], "200")
        self.assertEqual(result[0]["confirmed_refund"], "Unknown")

    def test_conflicting_duplicate_or_currency_cannot_choose_a_winner(self):
        first, second = self.row(), self.row(currency_id="ARS")
        result = kernel.payment_summary([first, second], "USD")[0]
        self.assertEqual(result["credited"], "Unknown")
        self.assertIn("conflict", result["reported_status"])
        self.assertEqual(kernel.payment_summary([second], "USD")[0]["credited"], "Unknown")

    def test_partially_refunded_is_not_accredited_and_no_inferred_refund(self):
        row = self.row(status_detail="partially_refunded", transaction_amount_refunded=20)
        result = kernel.payment_summary([row], "USD")[0]
        self.assertEqual((result["credited"], result["confirmed_refund"]), ("Unknown", "Unknown"))

    def test_confirmed_refund_requires_explicit_identity_status_and_amount(self):
        refund = {"id": 8, "payment_id": 7, "status": "approved", "amount": 50}
        row = self.row(status="refunded", refunds=[refund, copy.deepcopy(refund)])
        self.assertEqual(kernel.payment_summary([row], "USD")[0]["confirmed_refund"], "50")
        row["full_payment"]["refunds"][1]["status"] = "pending"
        self.assertEqual(kernel.payment_summary([row], "USD")[0]["confirmed_refund"], "Unknown")

    def test_wrong_identity_stale_missing_timestamp_and_invalid_snapshot_are_unknown(self):
        for body in ({"id": 8}, {"date_last_updated": None}):
            self.assertEqual(kernel.payment_summary([self.row(**body)], "USD")[0]["credited"], "Unknown")
        row = self.row()
        row["full_payment"] = "not a response"
        self.assertEqual(kernel.payment_summary([row], "USD")[0]["credited"], "Unknown")

    def test_json_and_legacy_repr_snapshots_do_not_leak_raw_data(self):
        row = self.row(private_url="never-render")
        for captured in (json.dumps(row["full_payment"]), repr(row["full_payment"])):
            row["full_payment"] = captured
            result = kernel.payment_summary([row], "USD")
            self.assertEqual(result[0]["credited"], "200")
            self.assertNotIn("never-render", repr(result))


class LocalVisibilityRegressionTests(unittest.TestCase):
    """Execute the actual compute; these doubles are not record-rule proof."""
    class Rows(list):
        @property
        def ids(self):
            return [row.id for row in self]

        def has_access(self, operation):
            return True

        def check_access(self, operation):
            return None

        def filtered(self, predicate):
            return type(self)(row for row in self if predicate(row))

        def mapped(self, name):
            values = [getattr(row, name) for row in self]
            return type(self)(dict.fromkeys(values)) if values and hasattr(values[0], "id") else values

    def compute(self, hidden=False):
        path = bridge_source("cancellation.py")
        tree = ast.parse(path.read_text(encoding="utf8"))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == "_compute_meli_commerce_summary")
        node.decorator_list = []
        class Markup(str):
            """Formatting boundary only; escaping is covered by actual native code."""
            pass
        class Cursor:
            def execute(self, *args):
                pass
            def fetchone(self):
                return (hidden,)
        class Environment(dict):
            cr = Cursor()
        env = Environment({"sale.order.line": self.Rows()})
        company = SimpleNamespace(id=1, mercadolibre_seller_id="42", mercadolibre_seller_user=5)
        root = SimpleNamespace(id=7, check_access=lambda action: None, env=env,
            _fields={"meli_orders": SimpleNamespace(relation="sale_order_mercadolibre_orders_rel",
                     column1="sale_order_id", column2="mercadolibre_orders_id")},
            company_id=company, state="draft", amount_total=200, currency_id=SimpleNamespace(name="USD"),
            meli_order_id="pack:900", picking_ids=[], invoice_ids=[])
        child = SimpleNamespace(id=101, order_id="101", pack_id="900", status="paid", status_detail="",
            company_id=company, sale_order=root, seller=repr({"id": 42}), seller_id=5, paid_amount=200,
            payments=self.Rows())
        class Source(SimpleNamespace):
            __hash__ = object.__hash__
        source = Source(id=201, quantity=2, unit_price="100", currency_id="USD", order_id=child)
        class Line(SimpleNamespace):
            def __getitem__(self, name):
                return getattr(self, name)
        line = Line(id=301, meli_source_order_item_id=source, product_uom_qty=2, price_unit=100,
                    price_total=200, display_type=False, discount=0, currency_id=root.currency_id,
                    product_id=SimpleNamespace(id=2), tax_ids=[])
        root.currency_id.compare_amounts = lambda first, second: round(first - second, 2)
        root.order_line = self.Rows([] if hidden else [line])
        root.meli_orders = self.Rows([] if hidden else [child])
        scope = {"Markup": Markup, "SOURCE": "meli_source_order_item_id", "ast": ast,
                 "remote_id": kernel.remote_id, "AdmissionError": kernel.AdmissionError,
                 "AccessError": PermissionError, "ValidationError": ValueError,
                 "payment_summary": kernel.payment_summary, "amount": kernel.amount,
                 "ml_product_price_conversion": lambda line, product, price, **kwargs: float(price)}
        # Bind genuine module helpers when present, never a fabricated producer.
        scope["local_commercial_state"] = getattr(kernel, "local_commercial_state", None)
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef):
                exec(compile(ast.Module([fn], []), str(path), "exec"), scope)
        if "sql" in path.read_text(encoding="utf8"):
            class SQL(str):
                def format(self, *values, **keywords):
                    return self
            scope["sql"] = SimpleNamespace(SQL=SQL, Identifier=lambda name: name)
        exec(compile(ast.fix_missing_locations(ast.Module([node], [])), str(path), "exec"), scope)
        class Roots(list):
            pass
        roots = Roots([root]); roots.env = env
        scope["_compute_meli_commerce_summary"](roots)
        return str(root.meli_commerce_summary)

    def test_visible_paid_group_has_explicit_active_local_aggregate(self):
        self.assertIn("Local commercial aggregate: active", self.compute())

    def test_no_visible_members_does_not_hide_physical_hidden_members(self):
        self.assertIn("reconciliation_pending", self.compute(hidden=True))


class ExistingOwnWrapperTests(unittest.TestCase):
    """Run the actual bridge wrapper/anchors; these doubles are not ORM proof."""
    def receiver(self, body, result=None, failure=None, root=False, seller=42):
        path = bridge_source("cancellation.py")
        tree = ast.parse(path.read_text(encoding="utf8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "SourceCancellation")
        scope = {"ast": ast, "remote_id": kernel.remote_id, "AdmissionError": kernel.AdmissionError,
                 "AccessError": PermissionError, "ValidationError": ValueError,
                 "CANCELLATION_STATES": kernel.CANCELLATION_STATES}
        from test_meli_t4_pack_commerce import admission
        scope["snapshot_response"] = admission.snapshot_response
        class Receiver(SimpleNamespace):
            def ensure_one(self):
                pass
            def check_access(self, mode):
                pass
        company, user = object(), object()
        receiver = Receiver(id=1, env=SimpleNamespace(company=company, user=user), company_id=company,
            seller_id=user, seller=repr({"id": seller}), order_id="101", pack_id="900", sale_order=root,
            _meli_assert_order_admission_actor=lambda config: "42")
        for node in cls.body:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
                exec(compile(ast.fix_missing_locations(ast.Module([node], [])), str(path), "exec"), scope)
                setattr(receiver, node.name, MethodType(scope[node.name], receiver))
        calls = []
        def project(values, **kwargs):
            calls.append(values)
            if failure:
                raise failure
            return result or {"commercial_group": "projected"}
        receiver.orders_update_order_json = project
        class Client:
            access_token = None
            status_code = last_status_code = 200
            def __init__(self):
                self.calls = []
            def get(self, path, params):
                self.calls.append(path)
                return self
            def json(self):
                return body
        return receiver, company, Client(), calls

    def test_known_own_rootless_paid_wrapper_reaches_existing_projection(self):
        receiver, company, client, calls = self.receiver(payload())
        try:
            self.assertEqual(receiver.orders_update_order(config=company, meli=client), [])
        except ValueError as exc:
            self.fail("ROOTLESS_PAID_FIRST_PROJECTION_BLOCKED: " + str(exc))
        self.assertEqual(client.calls, ["/orders/101"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(receiver.sale_order)

    def test_rootless_paid_wrapper_preserves_original_late_pack_error(self):
        failure = RuntimeError("synthetic late group projection failure")
        receiver, company, client, calls = self.receiver(payload(), failure=failure)
        try:
            receiver.orders_update_order(config=company, meli=client)
        except RuntimeError as exc:
            self.assertIs(exc, failure)
            self.assertTrue(exc._meli_pack_operation_failure)
        except ValueError as exc:
            self.fail("WRAPPER_NEVER_REACHED_LATE_GROUP_FAILURE: " + str(exc))
        else:
            self.fail("The original late error must escape")
        self.assertEqual(client.calls, ["/orders/101"])
        self.assertEqual(len(calls), 1)

    def test_cancellation_anchor_still_refuses_rootless_history(self):
        receiver, company, client, calls = self.receiver(payload())
        with self.assertRaisesRegex(ValueError, "existing commercial root"):
            receiver._meli_cancellation_anchor(company)
        self.assertEqual((client.calls, calls), ([], []))

    def test_foreign_seller_refuses_wrapper_before_get(self):
        receiver, company, client, calls = self.receiver(payload(), seller=43)
        with self.assertRaises(ValueError):
            receiver.orders_update_order(config=company, meli=client)
        self.assertEqual((client.calls, calls), ([], []))


class LocalCommercialStateTests(unittest.TestCase):
    def child(self, status="paid", quantity=2, audit=False):
        return {"status": status, "original_known": True,
                "quantities": [(2, quantity)], "applied_audit": audit}

    def test_active_partial_and_final_are_distinct_from_native_state(self):
        paid, cancelled = self.child(), self.child("cancelled", 0, True)
        self.assertEqual(kernel.local_commercial_state("draft", [paid]), "active")
        self.assertEqual(kernel.local_commercial_state("draft", [cancelled, paid]), "partially_cancelled")
        self.assertEqual(kernel.local_commercial_state("cancel", [cancelled], amount_zero=True), "cancelled")

    def test_unknown_hidden_and_missing_audit_remain_pending(self):
        for children, complete in (([], True), ([self.child()], False),
                ([dict(self.child(), original_known=False)], True),
                ([self.child("cancelled", 0)], True)):
            self.assertEqual(kernel.local_commercial_state("draft", children, complete=complete),
                             "reconciliation_pending")

    def test_nonterminal_processed_and_unexplained_quantities_remain_pending(self):
        for child in (self.child("pending_cancel"), self.child("partially_refunded"),
                      self.child("cancelled"), self.child(quantity=0), self.child(quantity=1)):
            self.assertEqual(kernel.local_commercial_state("sale", [child]), "reconciliation_pending")

    def test_root_state_and_zero_amount_must_match_final_cancellation(self):
        cancelled = self.child("cancelled", 0, True)
        for state, zero in (("draft", True), ("cancel", False), ("sale", True)):
            self.assertEqual(kernel.local_commercial_state(state, [cancelled], amount_zero=zero),
                             "reconciliation_pending")
        self.assertEqual(kernel.local_commercial_state("cancel", [cancelled, self.child()], amount_zero=True),
                         "reconciliation_pending")


class ControlledPriceBoundaryTests(unittest.TestCase):
    def test_quantity_zero_write_explicitly_preserves_source_price(self):
        path = bridge_source("order_line_provenance.py")
        node = next(node for node in ast.walk(ast.parse(path.read_text(encoding="utf8")))
                    if isinstance(node, ast.FunctionDef) and node.name == "_meli_source_cancel_quantity")
        writes = [call for call in ast.walk(node) if isinstance(call, ast.Call)
                  and isinstance(call.func, ast.Attribute) and call.func.attr == "write"
                  and isinstance(call.func.value, ast.Call) and isinstance(call.func.value.func, ast.Name)
                  and call.func.value.func.id == "super"]
        self.assertEqual(len(writes), 1)
        names = {key.value for key in writes[0].args[0].keys}
        self.assertIn("product_uom_qty", names)
        self.assertIn("price_unit", names, "QTY_ZERO_SCHEDULES_INHERITED_REPRICING")


class CancellationBannerAccessTests(unittest.TestCase):
    """Run the actual computes; native web_read/ACL proof is separate."""
    def sale(self, restricted=False, **values):
        class Sale(SimpleNamespace):
            @property
            def meli_orders(self):
                self.relation_reads += 1
                if restricted:
                    raise PermissionError("mercadolibre.orders read denied")
                return self.children
        class Documents(LocalVisibilityRegressionTests.Rows):
            def __sub__(self, other):
                return type(self)(row for row in self if row not in other)
        fields = dict(meli_order_id=False, meli_status=False, meli_status_detail=False,
                      state="draft", children=[], relation_reads=0,
                      picking_ids=Documents(), invoice_ids=Documents())
        fields.update(values)
        return Sale(**fields)

    def compute(self, sale, field):
        from html import escape
        import re
        scope = {"AccessError": PermissionError, "re": re, "Markup": str,
                 "html_escape": escape, "_MELI_CANCEL_CODES_ES": {},
                 "_MELI_REQUESTED_BY_ES": {}}
        try:
            method("_compute_" + field, extra_scope=scope)([sale])
        except PermissionError:
            self.fail("COMMON_BANNER_READ_REQUIRES_RESTRICTED_ML_RELATION: " + field)
        return getattr(sale, field)

    def test_unrelated_sale_without_ml_access_short_circuits(self):
        sale = self.sale(restricted=True)
        for field in ("meli_cancel_banner", "meli_cancel_pending_banner"):
            with self.subTest(field=field):
                self.assertFalse(self.compute(sale, field))
        self.assertEqual(sale.relation_reads, 0)

    def test_active_ml_sale_without_ml_access_short_circuits(self):
        sale = self.sale(restricted=True, meli_order_id="101", meli_status="paid")
        for field in ("meli_cancel_banner", "meli_cancel_pending_banner"):
            with self.subTest(field=field):
                self.assertFalse(self.compute(sale, field))
        self.assertEqual(sale.relation_reads, 0)

    def test_unreadable_cancellation_members_hide_only_the_banner(self):
        for field, state in (("meli_cancel_banner", "cancel"),
                             ("meli_cancel_pending_banner", "draft")):
            with self.subTest(field=field):
                sale = self.sale(restricted=True, state=state, meli_order_id="101",
                    meli_status="cancelled", meli_status_detail="buyer_cancel: private reason")
                self.assertFalse(self.compute(sale, field))
                self.assertEqual(sale.relation_reads, 1)

    def test_authorized_individual_rendering_and_pack_suppression_remain(self):
        child = SimpleNamespace(pack_id=False)
        for field, state, label in (("meli_cancel_banner", "cancel", "CANCELADO ML"),
                                   ("meli_cancel_pending_banner", "draft", "ACCIÓN REQUERIDA")):
            with self.subTest(field=field):
                values = dict(state=state, meli_order_id="101", meli_status="cancelled",
                              meli_status_detail="buyer_cancel: <reason>", children=[child])
                banner = self.compute(self.sale(**values), field)
                self.assertIn(label, banner)
                self.assertIn("&lt;reason&gt;", banner)
                for changes in ({"meli_order_id": "pack:900"}, {"children": [child, child]},
                                {"children": [SimpleNamespace(pack_id="900")]}):
                    self.assertFalse(self.compute(self.sale(**{**values, **changes}), field))


if __name__ == "__main__":
    unittest.main()
