"""Public known-pack resync routing with actual method bodies and local doubles.

ORM, ACL, audit and database rollback are independent native responsibilities.
"""
import ast
import copy
import datetime
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest

from test_meli_t4_pack_commerce import method
from test_meli_t4_pack_commerce import admission, payload

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT.parent / "odoo-t4-c-05-shared-resync/addons/viarengo_circuit_meli/models/shared_resync.py"


class Rows(list):
    @property
    def ids(self):
        return [row.id for row in self]

    def check_access(self, mode):
        return None

    def mapped(self, name):
        return Rows(getattr(row, name) for row in self)

    def filtered(self, callback):
        return Rows(row for row in self if callback(row))

    def __getattr__(self, name):
        if len(self) == 1:
            return getattr(self[0], name)
        raise AttributeError(name)

    def __getitem__(self, key):
        if isinstance(key, str):
            return getattr(self, key)
        return super().__getitem__(key)


def adapter(name):
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    node = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    scope = {"ValidationError": ValueError, "AccessError": PermissionError, "SOURCE": "meli_source_order_item_id",
             "deepcopy": copy.deepcopy,
             "CANCELLATION_STATES": __import__("test_meli_t4_cancellation").kernel.CANCELLATION_STATES,
             "remote_id": __import__("test_meli_t4_pack_commerce").admission.remote_id,
             "amount": __import__("test_meli_t4_cancellation").kernel.amount,
             "snapshot_response": __import__("test_meli_t4_pack_commerce").admission.snapshot_response}
    scope["line_population_incomplete"] = lambda *args: False
    exec(compile(ast.fix_missing_locations(ast.Module([node], [])), str(BRIDGE), "exec"), scope)
    return scope[name]


class PublicSharedResyncTests(unittest.TestCase):
    def receiver(self, result=None, late=None):
        company = SimpleNamespace(id=1, _fields={}, mercadolibre_cron_get_orders_status=True)
        root = SimpleNamespace(id=10, meli_order_id="pack:900")
        first = SimpleNamespace(id=1, order_id="101", pack_id="900", sale_order=root, status="paid",
                                source_quantity=2, source_line=21)
        second = SimpleNamespace(id=2, order_id="102", pack_id="900", sale_order=root, status="paid",
                                 source_quantity=3, source_line=22)
        root.meli_orders = Rows([first, second])
        source = SimpleNamespace(order_id=first, quantity=4, unit_price="125")
        class Line(SimpleNamespace):
            def __getitem__(self, key):
                return getattr(self, key)
        root.order_line = Rows([Line(meli_source_order_item_id=source, product_uom_qty=4, price_unit=125, price_total=500)])
        root.amount_total, root.state, root.picking_ids, root.invoice_ids = 500, "draft", [], []
        root.locked, root.meli_update_forbidden = False, False
        root.order_line[0].move_ids, root.order_line[0].qty_delivered, root.order_line[0].qty_invoiced = [], 0, 0
        root.currency_id = SimpleNamespace(compare_amounts=lambda a, b: 0 if a == b else 1)
        first._meli_cancel_population = lambda config: root.meli_orders
        calls, checkpoints, selected = [], [], Rows([first])
        state = {"value": 375, "sentinel": "caller"}
        @contextmanager
        def savepoint():
            before = copy.deepcopy(state)
            try:
                yield
            except BaseException:
                state.clear(); state.update(before)
                raise
        class Client:
            access_token = None
            needlogin_state = False
            status_code = last_status_code = 200
            def get(self, path, params):
                calls.append(path)
                self.body = {"id": 101, "status": "paid", "pack_id": 900,
                             "order_items": [{"quantity": 4, "unit_price": 125}]}
                return self
            def json(self):
                return self.body
        client = Client()
        class Env(dict):
            user = SimpleNamespace(company_id=company)
            cr = SimpleNamespace(savepoint=savepoint)
        def forbidden(*args, **kwargs):
            raise AssertionError("No legacy commit/global rollback/Autocommit may run")
        def factory(config):
            calls.append("FACTORY")
            checkpoints.append(("factory",))
            return client
        env = Env({"meli.util": SimpleNamespace(get_new_instance=factory)})
        def search(domain, **kwargs):
            checkpoints.append(("selector", domain, kwargs))
            return selected
        def preflight(candidates, config, account):
            checkpoints.append(("preflight", list(candidates)))
            if account is not None or any(not row.pack_id or not row.source_line for row in candidates):
                raise ValueError("Unsupported selector")
            return candidates
        def project(data, **kwargs):
            state["value"] = 500
            if late:
                raise late
            return ({"mixed_pack_update": "applied", "sale_order_id": 10, "order_id": "101",
                     "active_total": 500, "error": False} if result is None else result)
        scope = {"fields": SimpleNamespace(Datetime=SimpleNamespace(now=lambda: datetime.datetime(2026, 10, 17))),
                 "timedelta": datetime.timedelta, "Autocommit": forbidden, "MeliCommit": forbidden,
                 "MeliRollback": forbidden, "_logger": SimpleNamespace(info=lambda *a: None, error=lambda *a, **k: None)}
        class Probe:
            pass
        probe = Probe()
        probe.env, probe._fields = env, {"company_id": True}
        probe.search = search
        probe._meli_assert_order_admission_actor = lambda config: "42"
        probe._meli_assert_individual_sale_root = lambda *a: (_ for _ in ()).throw(ValueError("Unbound shared root"))
        probe.orders_update_order_json = project
        probe._meli_preflight_known_pack_resync = preflight
        if BRIDGE.exists():
            probe._meli_resync_known_pack_status = adapter("_meli_resync_known_pack_status").__get__(probe)
            probe._meli_checked_resync_result = adapter("_meli_checked_resync_result").__get__(probe)
        probe.orders_resync_status = method("orders_resync_status", extra_scope=scope).__get__(probe)
        return probe, company, client, calls, checkpoints, state, selected, second

    def test_existing_public_resync_reaches_same_status_paid_projection(self):
        probe, company, client, calls, checks, state, _, _ = self.receiver()
        try:
            result = probe.orders_resync_status(meli=client, config=company)
        except ValueError as exc:
            self.fail("EXISTING_KNOWN_PACK_RESYNC_BARRIER: " + str(exc))
        self.assertEqual(state["value"], 500)
        self.assertEqual(result["results"][0]["result"]["mixed_pack_update"], "applied")
        self.assertEqual(calls, ["/orders/101"])
        self.assertEqual(checks[0][2], {"order": "date_created asc", "limit": 500})
        self.assertEqual(state["sentinel"], "caller")

    def test_late_failure_keeps_original_object_and_owned_savepoint(self):
        failure = RuntimeError("Late selected producer failure")
        probe, company, client, _, _, state, _, _ = self.receiver(late=failure)
        caught = None
        try:
            probe.orders_resync_status(meli=client, config=company)
        except Exception as exc:
            caught = exc
        self.assertIs(caught, failure)
        self.assertTrue(caught._meli_pack_operation_failure)
        self.assertEqual(state, {"value": 375, "sentinel": "caller"})

    def test_false_or_unknown_result_rolls_back_owned_operation(self):
        for result in (None, False, [], {}, {"sale_order_id": 99},
                       {"sale_order_id": 10, "error": "unhandled"}):
            with self.subTest(result=result):
                probe, company, client, _, _, state, _, _ = self.receiver()
                def false_projection(*args, **kwargs):
                    state["value"] = 999
                    return result
                probe.orders_update_order_json = false_projection
                with self.assertRaises(ValueError):
                    probe.orders_resync_status(meli=client, config=company)
                self.assertEqual(state, {"value": 375, "sentinel": "caller"})

    def test_partial_current_response_is_pending_without_projection(self):
        probe, company, client, calls, _, state, _, _ = self.receiver()
        client.status_code = client.last_status_code = 206
        result = probe.orders_resync_status(meli=client, config=company)
        self.assertEqual(result["results"], [{"order_id": "101", "outcome": "pending",
            "error": "complete_current_observation_unavailable"}])
        self.assertEqual(state["value"], 375)
        self.assertEqual(calls, ["/orders/101"])

    def test_wrong_returned_identity_refuses_before_producer(self):
        probe, company, client, _, _, state, _, _ = self.receiver()
        get = client.get
        def wrong(path, params):
            result = get(path, params)
            result.body["id"] = 999
            return result
        client.get = wrong
        with self.assertRaises(ValueError):
            probe.orders_resync_status(meli=client, config=company)
        self.assertEqual(state["value"], 375)

    def test_whole_selector_denied_before_even_factory(self):
        probe, company, _, calls, _, state, selected, second = self.receiver()
        second.pack_id = False
        selected.append(second)
        with self.assertRaises(ValueError):
            probe.orders_resync_status(config=company)
        self.assertEqual(calls, [])
        self.assertEqual(state["value"], 375)

    def test_unsupported_account_refuses_before_factory(self):
        probe, company, _, calls, _, _, _, _ = self.receiver()
        with self.assertRaises(ValueError):
            probe.orders_resync_status(config=company, account=SimpleNamespace(id=99))
        self.assertEqual(calls, [])

    def test_configured_selector_days_limit_order_and_exclusions_stay(self):
        probe, company, client, _, checks, _, _, _ = self.receiver()
        company._fields = {"mercadolibre_cron_orders_status_days": True,
                           "mercadolibre_cron_orders_status_limit": True}
        company.mercadolibre_cron_orders_status_days = 7
        company.mercadolibre_cron_orders_status_limit = 12
        probe.orders_resync_status(meli=client, config=company)
        self.assertEqual(checks[0][2], {"order": "date_created asc", "limit": 12})
        domain = checks[0][1]
        self.assertIn(("date_created", ">=", datetime.datetime(2026, 10, 10)), domain)
        self.assertIn(("sale_order.state", "!=", "cancel"), domain)
        self.assertIn(("status", "not in", ("cancelled", "invalid")), domain)
        self.assertIn(("shipment_status", "not in", ("delivered",)), domain)
        self.assertIn(("company_id", "in", (1, False)), domain)

    def test_actual_configured_company_caller_defers_factory_until_preflight(self):
        probe, company, _, calls, checks, _, _, _ = self.receiver()
        probe.env["mercadolibre.orders"] = probe
        tree = ast.parse((ROOT / "models/company.py").read_text(encoding="utf-8"))
        node = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                    and node.name == "cron_meli_orders_status")
        scope = {}
        exec(compile(ast.Module([node], []), "actual-company-caller", "exec"), scope)
        caller = SimpleNamespace(env=probe.env)
        company.mercadolibre_cron_get_orders_status = False
        scope[node.name](caller)
        self.assertEqual(calls, [])
        company.mercadolibre_cron_get_orders_status = True
        result = scope[node.name](caller)
        self.assertEqual(result["results"][0]["outcome"], "verified")
        self.assertEqual([row[0] for row in checks], ["selector", "preflight", "factory"])
        self.assertEqual(calls, ["FACTORY", "/orders/101"])

    def test_login_guard_is_explicit_pending_not_success(self):
        probe, company, client, calls, _, state, _, _ = self.receiver()
        client.needlogin_state = True
        result = probe.orders_resync_status(meli=client, config=company)
        self.assertEqual(result["results"][0]["outcome"], "pending")
        self.assertEqual(calls, [])
        self.assertEqual(state["value"], 375)

    def ordered_receiver(self, status="cancelled", split=False, processed=False, incomplete=False):
        probe, company, client, calls, checks, state, selected, second = self.receiver()
        first = selected[0]
        selected.append(second)
        root = first.sale_order
        root.state = "sale" if processed else "draft"
        first_body, second_body = payload(101, 4, 125), payload(102, 3, 125)
        second_body.update(status=status, cancel_detail={"code": "pack_splitted" if split else "buyer_cancelled"})
        if status == "paid":
            second_body.pop("cancel_detail")
        bodies = {"/orders/101": first_body, "/orders/102": second_body}
        projected, audits = [], []
        def get(path, params):
            calls.append(path)
            client.status_code = client.last_status_code = 206 if incomplete and path.endswith("102") else 200
            client.body = copy.deepcopy(bodies[path])
            return client
        client.get = get
        def project(data, **kwargs):
            oid = str(data["order_json"]["id"])
            projected.append(oid)
            if oid == "102":
                second.status = status
                reason = "processed" if processed else "nonterminal" if status != "cancelled" or split else None
                audits.append(reason or "applied")
                if not reason:
                    state["value"] = 200
                return {"sale_order_id": 10, "order_id": oid,
                        "cancellation": "reconciliation" if reason else "applied", "error": reason or False}
            if second.status == "paid":
                # Execute the real current all-paid validation contract, not a
                # missing helper or fabricated exception as causal RED.
                admission.verify_pack_commerce({"id": 900, "status": "released", "status_detail": None,
                    "buyer": {"id": 50}, "shipment": None, "orders": [{"id": 101}, {"id": 102}]},
                    [first_body, second_body], "42")
            if incomplete and status == "paid":
                state["value"] = 500
                return {"sale_order_id": 10, "commercial_group": "draft", "verified_order_ids": ["101", "102"]}
            if processed or status != "cancelled" or split:
                raise ValueError("Paid economics has no compatible contract on this root")
            state["value"] = 500
            return {"sale_order_id": 10, "order_id": oid, "mixed_pack_update": "applied", "error": False}
        probe.orders_update_order_json = project
        # Audit/ORM postcondition checks are separate native proof; here the
        # actual public/batch control flow and all-paid kernel are exercised.
        probe._meli_checked_resync_result = lambda order, body, result, config: {
            "outcome": "reconciliation" if result.get("error") else "verified", "result": result}
        return probe, company, client, calls, state, projected, audits

    def test_complete_selector_routes_newer_cancel_before_older_paid_projection(self):
        probe, company, client, calls, state, projected, audits = self.ordered_receiver()
        try:
            result = probe.orders_resync_status(meli=client, config=company)
        except ValueError as exc:
            self.fail("COMPLETE_SELECTOR_ORDERING_BARRIER: " + str(exc))
        self.assertEqual(projected, ["102", "101"])
        self.assertEqual(calls, ["/orders/101", "/orders/102"])
        self.assertEqual(audits, ["applied"])
        self.assertEqual(state, {"value": 500, "sentinel": "caller"})
        self.assertEqual([row["order_id"] for row in result["results"]], ["101", "102"])

    def test_complete_selector_reconciliation_persists_and_paid_is_deferred(self):
        for status, split in (("pending_cancel", False), ("partially_refunded", False), ("cancelled", True)):
            with self.subTest(status=status, split=split):
                probe, company, client, _, state, projected, audits = self.ordered_receiver(status, split)
                try:
                    result = probe.orders_resync_status(meli=client, config=company)
                except ValueError as exc:
                    self.fail("RECONCILIATION_STARVED: " + str(exc))
                self.assertEqual(projected, ["102"])
                self.assertEqual(audits, ["nonterminal"])
                self.assertEqual(state["value"], 375)
                self.assertEqual([row["outcome"] for row in result["results"]], ["deferred", "reconciliation"])

    def test_complete_processed_selector_keeps_reconciliation_without_paid_attempt(self):
        probe, company, client, _, state, projected, audits = self.ordered_receiver(processed=True)
        try:
            result = probe.orders_resync_status(meli=client, config=company)
        except ValueError as exc:
            self.fail("PROCESSED_RECONCILIATION_STARVED: " + str(exc))
        self.assertEqual((projected, audits, state["value"]), (["102"], ["processed"], 375))
        self.assertEqual([row["outcome"] for row in result["results"]], ["deferred", "reconciliation"])

    def test_incomplete_selected_snapshot_defers_paid_without_assuming_all_paid(self):
        probe, company, client, _, state, projected, audits = self.ordered_receiver(status="paid", incomplete=True)
        result = probe.orders_resync_status(meli=client, config=company)
        self.assertEqual((projected, audits, state["value"]), ([], [], 375))
        self.assertEqual([row["outcome"] for row in result["results"]], ["deferred", "pending"])

    def historical_receiver(self, malformed=False):
        probe, company, client, calls, checks, state, selected, second = self.receiver()
        class Record(SimpleNamespace):
            def __getitem__(self, name):
                return getattr(self, name)
        first, root = selected[0], selected[0].sale_order
        second = Record(**vars(second))
        root.meli_orders[1] = second
        second.status, second.status_detail = "cancelled", " | pack_splitted"
        second.total_amount = second.paid_amount = 375
        first.currency_id = second.currency_id = "USD"
        root.currency_id.name = "USD"
        first._meli_cancellation_anchor = second._meli_cancellation_anchor = lambda config: "42"
        first._meli_cancel_population = lambda config: root.meli_orders
        product = Record(id=50, company_id=False, uom_id=1)
        lines, sources = Rows(), Rows()
        for child, quantity, price in ((first, 4, 125), (second, 3, 125)):
            source = Record(id=child.id + 30, order_id=child, product_id=product,
                order_item_id="MLA1", order_item_variation_id=False,
                quantity=quantity, unit_price=price, currency_id="USD")
            sources.append(source)
            lines.append(Record(id=child.id + 40, meli_source_order_item_id=source,
                product_id=product, company_id=company, meli_order_item_id="MLA1",
                meli_order_item_variation_id=False, display_type=False, is_delivery=False,
                product_uom_id=1, product_uom_qty=quantity, price_unit=price, price_total=quantity * price,
                tax_ids=[], move_ids=[], qty_delivered=0, qty_invoiced=0))
        root.order_line, root.company_id = lines, company
        root.amount_total = 875
        target_snapshot = {"id": 42, "quantity": 3, "source_quantity": 3, "price": 125}
        captured_order = {"id": 2, "remote": "102", "status": "cancelled", "reported_total": 375, "reported_paid": 375}
        snapshot = {"root": 10, "state": "draft", "amount_total": 875,
                    "members": [1, 2], "lines": [target_snapshot], "orders": [captured_order]}
        document = {"evidence": {"status": "cancelled", "status_detail": "", "cancel_detail": {"code": "pack_splitted"},
                                "total_amount": "375", "paid_amount": "375"},
                    "outcome": "split_or_transformation_requires_reconciliation", "before": snapshot, "after": snapshot}
        second._name = "mercadolibre.orders"
        second.env = probe.env
        second._meli_cancel_snapshot = lambda *args: copy.deepcopy(snapshot)
        # Source routing contract only; actual canonical mail parser/ACL is a
        # registered native responsibility, never a fabricated source PASS.
        second._meli_audit_document = lambda *args: None if malformed else document
        if any(node.name == "_meli_resync_reconciled_positive" for node in ast.walk(ast.parse(BRIDGE.read_text(encoding="utf-8"))) if isinstance(node, ast.FunctionDef)):
            second._meli_resync_reconciled_positive = adapter("_meli_resync_reconciled_positive").__get__(second)
        probe.env.flush_all = lambda: None
        probe.env.cr.execute = lambda *args: None
        probe.env.cr.fetchone = lambda: (False,)
        probe.env["mercadolibre.order_items"] = SimpleNamespace(search=lambda domain: sources)
        probe.env["mail.message"] = SimpleNamespace(search=lambda domain: [object()])
        probe._meli_preflight_known_pack_resync = adapter("_meli_preflight_known_pack_resync").__get__(probe)
        return probe, company, client, calls, state

    def test_canonical_reconciliation_history_defers_without_refetching_excluded_child(self):
        probe, company, client, calls, state = self.historical_receiver()
        try:
            result = probe.orders_resync_status(meli=client, config=company)
        except ValueError as exc:
            self.fail("CANONICAL_RECONCILIATION_HISTORY_BARRIER: " + str(exc))
        self.assertEqual(result["results"][0]["outcome"], "deferred")
        self.assertEqual(calls, ["/orders/101"])
        self.assertEqual(state, {"value": 375, "sentinel": "caller"})

    def test_malformed_reconciliation_history_refuses_before_factory(self):
        probe, company, _, calls, state = self.historical_receiver(malformed=True)
        with self.assertRaises(ValueError):
            probe.orders_resync_status(config=company)
        self.assertEqual(calls, [])
        self.assertEqual(state["value"], 375)


if __name__ == "__main__":
    unittest.main()
