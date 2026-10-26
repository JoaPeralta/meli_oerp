"""Execute real entry/admission code and bounded commercial snapshot rules.

These source doubles do not substitute for registered ORM/PostgreSQL proof.
"""
import ast
import copy
import datetime
import importlib.util
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pack_admission", ROOT / "models/meli_pack_admission.py")
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)


def payload(oid=101, quantity=2, price=100, shipping=None):
    return {"id": oid, "pack_id": 900, "seller": {"id": 42}, "buyer": {"id": 50},
            "status": "paid", "currency_id": "USD", "site_id": "MLA", "tags": [],
            "total_amount": quantity * price, "paid_amount": quantity * price,
            "coupon": {"amount": 0}, "shipping": shipping, "payments": [],
            "context": {"channel": "marketplace"},
            "order_items": [{"item": {"id": "MLA123", "variation_id": 7},
                             "quantity": quantity, "unit_price": price, "currency_id": "USD"}]}


def method(name, prefix=False, extra_scope=None):
    node = next(n for n in ast.walk(ast.parse((ROOT / "models/orders.py").read_text(encoding="utf8")))
                if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    if prefix:
        stop = next(i for i, n in enumerate(node.body) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "saleorder_obj" for t in n.targets))
        node.body = node.body[:stop]
    scope = {"AdmissionError": admission.AdmissionError, "ValidationError": ValueError,
             "verify_order": admission.verify_order, "remote_id": admission.remote_id,
             "snapshot_response": admission.snapshot_response, "pack_order_ids": admission.pack_order_ids,
              "verify_pack_commerce": admission.verify_pack_commerce,
              "fetch_pack_snapshot": admission.fetch_pack_snapshot}
    scope.update(extra_scope or {})
    exec(compile(ast.fix_missing_locations(ast.Module([node], [])), "actual-orders-entry", "exec"), scope)
    return scope[name]


class EntryBarrierTests(unittest.TestCase):
    def entry(self, shipping=None):
        current, sibling = payload(shipping=shipping), payload(102, 3, 125, shipping)
        company = SimpleNamespace(id=1, _fields={})
        shipment = SimpleNamespace(_meli_admit_shipment_orders=lambda *args: {
            "orders": [current, sibling], "complete": True, "pending": None,
            "associations": [], "responses": []})
        class Environment(dict):
            context = {}
        env = Environment({"mercadolibre.shipment": shipment})
        class OrderProbe(SimpleNamespace):
            def __iter__(self):
                yield self
        probe = OrderProbe(env=env, id=1, order_id="101", seller=repr({"id": 42}),
            pack_id="900", shipping_id=False, sale_order=False,
            _meli_assert_order_admission_actor=lambda config: "42",
            _meli_preflight_order_history=lambda *args, **kwargs: None,
            _meli_import_pack=lambda *args, **kwargs: {"commercial_group": "projected"})
        probe._meli_order_admission = lambda *args: method("_meli_order_admission")(probe, *args)
        return method("orders_update_order_json", prefix=True)(probe,
            {"id": False, "remote_order_id": "101", "order_json": current},
            config=company, meli=object())

    def test_fresh_customer_selection_precedes_existing_mapping_requirement(self):
        # Execute the real producer through customer selection; an already verified
        # fresh buyer cannot have an existing principal before its owned projection.
        node = next(n for n in ast.walk(ast.parse((ROOT / "models/orders.py").read_text()))
                    if isinstance(n, ast.FunctionDef) and n.name == "_meli_import_pack")
        node.decorator_list = []
        body = node.body[1].body if isinstance(node.body[0], ast.Expr) else node.body[0].body
        stop = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "ids" for t in n.targets))
        body[:] = body[:stop] + [ast.Return(ast.Name("partner", ast.Load()))]
        class Record(list):
            _name = "res.partner"
            property_account_position_id = False
            parent_id = False
            active = True
            def check_access(self, mode):
                pass
            def flush_recordset(self, names):
                pass
            @property
            def ids(self):
                return [1] if self else []
        from contextlib import nullcontext
        company = SimpleNamespace(id=1, _fields={}, mercadolibre_pricelist=SimpleNamespace(
            currency_id=SimpleNamespace(name="USD")))
        class Environment(dict):
            pass
        env = Environment()
        env.company = company
        env.cr = SimpleNamespace(savepoint=nullcontext, execute=lambda *args: None, fetchone=lambda: (False,))
        principal = Record([1]); principal.env = env; principal.company_id = company
        principal.meli_buyer_id = "50"; principal.id = 1
        env["res.partner"] = SimpleNamespace(search=lambda domain: Record())
        buyer = Record(); buyer._name = "mercadolibre.buyers"; buyer.env = env
        selected = dict(principal=principal, invoice=principal, shipping=principal, buyer=buyer, metadata=False)
        calls = []
        def customer(*args):
            calls.append("customer")
            return selected
        scope = dict(AdmissionError=admission.AdmissionError, ValidationError=ValueError,
            AccessError=PermissionError, remote_id=admission.remote_id,
            models=SimpleNamespace(BaseModel=Record),
            fields=SimpleNamespace(Datetime=SimpleNamespace(to_datetime=lambda value: value)), ml_datetime=lambda value: value)
        exec(compile(ast.fix_missing_locations(ast.Module([node], [])), "actual-pack-customer-entry", "exec"), scope)
        source = payload(); source.update(date_created="2026-01-01", date_closed="2026-01-01")
        group = admission.verify_pack_commerce(pack(), [source, payload(102)], "42")
        for row in group["orders"]:
            row.update(date_created="2026-01-01", date_closed="2026-01-01")
        actor = SimpleNamespace(env=env, _meli_collect_pack_commerce=lambda *args: group,
            _meli_pack_customer_projection=customer)
        self.assertIs(scope[node.name](actor, source, company, object()), principal)
        self.assertEqual(calls, ["customer"])

    def test_verified_pack_with_shipment_reaches_explicit_group_path(self):
        result = self.entry({"id": 800})
        self.assertEqual(result, {"commercial_group": "projected"}, "EXISTING_PACK_BARRIER")

    def test_pack_without_shipment_reaches_explicit_group_validation(self):
        result = self.entry()
        self.assertEqual(result, {"commercial_group": "projected"}, "EXISTING_PACK_BARRIER")


def pack(shipping=None):
    return {"id": 900, "status": "released", "status_detail": None, "buyer": {"id": 50},
            "orders": [{"id": 101}, {"id": 102}], "shipment": shipping}


class SnapshotContractTests(unittest.TestCase):
    def setUp(self):
        self.pack = pack()
        self.sources = [payload(), payload(102, 3, 125)]

    def validate(self):
        return admission.verify_pack_commerce(self.pack, self.sources, "42")

    def test_same_publication_children_keep_exact_separate_economics(self):
        before = copy.deepcopy((self.pack, self.sources))
        group = self.validate()
        self.assertEqual((group["total"], group["paid"]), (575, 575))
        self.assertEqual([(row["id"], row["order_items"][0]["quantity"],
                           row["order_items"][0]["unit_price"]) for row in group["orders"]],
                         [(101, 2, 100), (102, 3, 125)])
        self.assertEqual((self.pack, self.sources), before)
        self.assertEqual(group["membership_scope"], "configured-seller-visible-pack")

    def test_child_update_changes_only_its_aggregate_contribution(self):
        before = copy.deepcopy(self.sources[1])
        self.sources[0] = payload(101, 4, 125)
        self.assertEqual(self.validate()["total"], 875)
        self.assertEqual(self.sources[1], before)

    def test_proven_shipment_does_not_supply_commercial_quantities(self):
        self.pack["shipment"] = {"id": 800}
        for source in self.sources:
            source["shipping"] = {"id": 800}
        self.assertEqual(self.validate()["shipping_id"], "800")
        self.assertEqual(self.validate()["total"], 575)

    def test_missing_duplicate_or_extra_snapshot_is_not_complete(self):
        for sources in (self.sources[:1], self.sources * 2, self.sources + [payload(103)]):
            with self.subTest(ids=[row["id"] for row in sources]), self.assertRaises(admission.AdmissionError):
                admission.verify_pack_commerce(self.pack, sources, "42")

    def test_cancelled_invalid_delivered_split_and_kit_children_refuse(self):
        changes = ({"status": "cancelled"}, {"status": "invalid"}, {"tags": ["delivered"]},
                   {"status_detail": "pack_splitted"}, {"cancel_detail": {"code": "bundle_component"}},
                   {"status": "payment_required"})
        for change in changes:
            rows = copy.deepcopy(self.sources)
            rows[1].update(change)
            with self.subTest(change=change), self.assertRaises(admission.AdmissionError):
                admission.verify_pack_commerce(self.pack, rows, "42")
        self.sources[1]["order_items"][0]["item"]["bundle"] = True
        with self.assertRaises(admission.AdmissionError):
            self.validate()

    def test_mismatched_seller_buyer_currency_channel_and_billing_refuse(self):
        for change in ({"seller": {"id": 43}}, {"buyer": {"id": 51}},
                       {"currency_id": "ARS"}, {"context": {"channel": "shop"}},
                       {"buyer": {"id": 50, "billing_info": {"id": 999}}}):
            rows = copy.deepcopy(self.sources)
            rows[1].update(change)
            with self.subTest(change=change), self.assertRaises(admission.AdmissionError):
                admission.verify_pack_commerce(self.pack, rows, "42")

    def test_discount_finance_shipping_total_and_price_transform_refuse(self):
        for change in ({"coupon": {"amount": 1}}, {"paid_amount": 1}, {"total_amount": 1},
                       {"financing_fee_amount": 1}, {"shipping": {"cost": 1}}):
            rows = copy.deepcopy(self.sources)
            rows[1].update(change)
            with self.subTest(change=change), self.assertRaises(admission.AdmissionError):
                admission.verify_pack_commerce(self.pack, rows, "42")
        self.sources[1]["order_items"][0]["full_unit_price"] = 200
        with self.assertRaises(admission.AdmissionError):
            self.validate()

    def test_missing_invalid_pack_shape_and_unreleased_status_refuse(self):
        for change in ({"shipment": []}, {"buyer": []}, {"status": "pending_cancel"},
                       {"orders": [{"id": 101}, {"id": 101}]}, {"orders": []}):
            document = dict(self.pack, **change)
            with self.subTest(change=change), self.assertRaises(admission.AdmissionError):
                admission.verify_pack_commerce(document, self.sources, "42")
        self.pack.pop("shipment")
        with self.assertRaises(admission.AdmissionError):
            self.validate()

    def test_inconsistent_or_unproven_shipment_refuses(self):
        self.pack["shipment"] = {"id": 800}
        with self.assertRaises(admission.AdmissionError):
            self.validate()
        self.sources[0]["shipping"] = {"id": 800}
        self.sources[1]["shipping"] = {"id": 801}
        with self.assertRaises(admission.AdmissionError):
            self.validate()


class MutableClient:
    access_token = None

    def __init__(self, sources):
        self.calls = []
        self.routes = {"/packs/900": (200, pack())}
        for source in sources:
            oid = str(source["id"])
            self.routes["/orders/" + oid] = (200, source)
            self.routes["/orders/" + oid + "/discounts"] = (200, {"details": []})

    def get(self, path, params=None):
        self.calls.append(path)
        self.status_code, body = self.routes[path]
        self.body, self.headers = copy.deepcopy(body), {}
        return self

    def json(self):
        return self.body


class CollectionContractTests(unittest.TestCase):
    def setUp(self):
        self.sources = [payload(), payload(102, 3, 125)]
        self.client = MutableClient(self.sources)
        self.local = ["101", "102"]
        self.admitted = {"orders": self.sources[:1], "complete": True,
                         "responses": [], "pending": "source_line_projection_pending_t4_bc"}
        self.probe = SimpleNamespace(
            _meli_assert_order_admission_actor=lambda config: "42",
            _meli_pack_local_orders=lambda *args: SimpleNamespace(mapped=lambda key: self.local),
            _meli_order_admission=lambda *args: self.admitted,
            _meli_preflight_order_history=lambda *args: None)

    def collect(self):
        return method("_meli_collect_pack_commerce")(self.probe, self.sources[0], object(), self.client)

    def test_exact_owned_local_group_is_refetched_and_verified(self):
        self.assertEqual(self.collect()["total"], 575)
        self.assertEqual(self.client.calls, ["/packs/900", "/orders/101", "/orders/101/discounts",
                                             "/orders/102", "/orders/102/discounts", "/packs/900"])

    def test_unknown_member_is_never_probed(self):
        self.local = ["101"]
        with self.assertRaisesRegex(ValueError, "discovery is pending"):
            self.collect()
        self.assertEqual(self.client.calls, ["/packs/900"])

    def test_partial_or_missing_membership_prevents_member_get(self):
        for field, value in (("complete", False), ("responses", [{"status": 206}])):
            self.admitted[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.collect()
            self.assertFalse(any(path.startswith("/orders/") for path in self.client.calls))

    def test_foreign_fresh_seller_cannot_be_associated(self):
        self.client.routes["/orders/102"][1]["seller"] = {"id": 43}
        with self.assertRaises(admission.AdmissionError):
            self.collect()
        self.assertNotIn("/orders/102/discounts", self.client.calls)

    def test_non200_pack_or_order_is_not_success(self):
        for path in ("/packs/900", "/orders/102"):
            old = self.client.routes[path]
            self.client.routes[path] = (206, old[1])
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.collect()
            self.client.routes[path] = old

    def test_nonempty_or_missing_discount_response_refuses(self):
        for body in ({}, {"details": [{}]}, []):
            self.client.routes["/orders/102/discounts"] = (200, body)
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.collect()

    def test_changed_visible_population_during_read_refuses(self):
        original = self.client.get
        def changed(path, params=None):
            response = original(path, params)
            if path == "/packs/900" and self.client.calls.count(path) == 2:
                response.body["orders"] = [{"id": 101}]
            return response
        self.client.get = changed
        with self.assertRaisesRegex(ValueError, "changed while collecting"):
            self.collect()


class GroupBoundaryStructureTests(unittest.TestCase):
    def test_group_uses_existing_exact_source_operations_and_never_scalar_recursion(self):
        source = ast.unparse(next(n for n in ast.walk(ast.parse((ROOT / "models/orders.py").read_text()))
                                  if isinstance(n, ast.FunctionDef) and n.name == "_meli_import_pack"))
        self.assertIn("_meli_upsert_source_item", source)
        self.assertIn("_meli_upsert_source_sale_line", source)
        self.assertIn("env.cr.savepoint", source)
        for forbidden in (".sudo(", ".commit(", ".rollback(", "MeliRollback", "confirm_ml(",
                          "fetch_shipment(", "orders_update_order_json(", "with_context("):
            self.assertNotIn(forbidden, source)


class ResyncSafetyTests(unittest.TestCase):
    def test_actual_legacy_resync_refuses_pack_before_child_or_whole_root_change(self):
        node = next(n for n in ast.walk(ast.parse((ROOT / "models/orders.py").read_text()))
                    if isinstance(n, ast.FunctionDef) and n.name == "orders_resync_status")
        node.decorator_list = []
        scope = {"ValidationError": ValueError, "timedelta": datetime.timedelta,
                 "fields": SimpleNamespace(Datetime=SimpleNamespace(now=datetime.datetime.now)),
                 "Autocommit": lambda *args: None, "MeliCommit": lambda *args: None,
                 "MeliRollback": lambda *args: None,
                 "_logger": SimpleNamespace(info=lambda *args: None, error=lambda *args, **kw: None)}
        exec(compile(ast.fix_missing_locations(ast.Module([node], [])), "actual-status-resync", "exec"), scope)
        cancellations = []
        root = SimpleNamespace(state="draft", meli_status_detail="", meli_order_id="pack:900",
                               meli_cancel_with_detail=lambda *args: cancellations.append(args))
        order = SimpleNamespace(order_id="101", pack_id="900", status="paid", status_detail="",
                                sale_order=root, orders_update_order=lambda **kw: None)
        company = SimpleNamespace(id=1, _fields={})
        model = SimpleNamespace(_fields={}, env=SimpleNamespace(user=SimpleNamespace(company_id=company)),
                                search=lambda *args, **kwargs: [order],
                                _meli_assert_order_admission_actor=lambda config: "42",
                                _meli_assert_individual_sale_root=lambda *args: None)
        incoming = dict(payload(), status="cancelled")
        # Connector-only execution has no source-bound bridge; keep the
        # anonymous shared-root case negative through the actual default seam.
        model._meli_resync_known_pack_status = method("_meli_resync_known_pack_status").__get__(model)
        client = SimpleNamespace(access_token=None, needlogin_state=False,
                                 get=lambda *args, **kwargs: SimpleNamespace(json=lambda: incoming))
        with self.assertRaisesRegex(ValueError, "Pack status"):
            scope[node.name](model, meli=client, config=company)
        self.assertEqual((order.status, order.status_detail, root.meli_status_detail, cancellations),
                         ("paid", "", "", []))


class LegacyCatchSafetyTests(unittest.TestCase):
    def assert_propagation(self, name, handler_name):
        node = next(n for n in ast.walk(ast.parse((ROOT / "models/orders.py").read_text()))
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        handlers = [handler for child in ast.walk(node) if isinstance(child, ast.Try)
                    for handler in child.handlers]
        handler = next(h for h in handlers if ast.unparse(h.type) == handler_name)
        error = RuntimeError("late group failure")
        rollbacks = []
        scope = {"error": error, "_pack_route": True, "self": object(),
                 "MeliRollback": lambda *args: rollbacks.append(True),
                 "order": SimpleNamespace(sale_order=False), "config": SimpleNamespace(_fields={}),
                 "_logger": SimpleNamespace(info=lambda *args: None, warning=lambda *args: None,
                                             error=lambda *args, **kwargs: None)}
        body = ast.Try(body=[ast.Raise(exc=ast.Name(id="error", ctx=ast.Load()))],
                       handlers=[ast.ExceptHandler(type=ast.Name(id="Exception", ctx=ast.Load()),
                                                  name="e", body=copy.deepcopy(handler.body))],
                       orelse=[], finalbody=[])
        loop = ast.For(target=ast.Name(id="_attempt", ctx=ast.Store()),
                       iter=ast.Tuple(elts=[ast.Constant(2)], ctx=ast.Load()), body=[body], orelse=[])
        function = ast.FunctionDef(name="real_catch", args=ast.arguments(posonlyargs=[], args=[],
            kwonlyargs=[], kw_defaults=[], defaults=[]), body=[loop], decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module([function], [])), "actual-legacy-catch", "exec"), scope)
        with self.assertRaisesRegex(RuntimeError, "late group failure") as caught:
            scope["real_catch"]()
        self.assertIs(caught.exception, error)
        self.assertEqual(rollbacks, [])

    def test_order_wrapper_preserves_escaping_group_failure(self):
        self.assert_propagation("orders_update_order", "Exception")

    def test_query_generic_catch_preserves_escaping_group_failure(self):
        self.assert_propagation("orders_query_iterate", "Exception")

    def test_query_serialization_catch_does_not_retry_or_rollback_group(self):
        self.assert_propagation("orders_query_iterate", "_serialization_ex")

    def test_local_routing_flag_confers_no_authority_and_detects_shared_history(self):
        helper = method("_meli_is_pack_route")
        ordinary = SimpleNamespace(pack_id=False, sale_order=False)
        shared = SimpleNamespace(pack_id=False, sale_order=SimpleNamespace(meli_order_id="101", meli_orders=[1, 2]))
        probe = SimpleNamespace(search=lambda domain: [ordinary])
        self.assertFalse(helper(probe, {"id": 101}))
        self.assertTrue(helper(probe, {"id": 101, "pack_id": 900}))
        self.assertTrue(helper(probe, {"id": 101}, [shared]))
        self.assertTrue(helper(probe, {"id": 101}, [SimpleNamespace(pack_id="900", sale_order=False)]))

    def test_query_route_flag_is_reset_for_each_child_before_retry_loop(self):
        node = next(n for n in ast.walk(ast.parse((ROOT / "models/orders.py").read_text()))
                    if isinstance(n, ast.FunctionDef) and n.name == "orders_query_iterate")
        population = next(n for n in ast.walk(node) if isinstance(n, ast.For)
                          and ast.unparse(n.target) == "order_json")
        flag = next(n for n in ast.walk(population) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_pack_route" for t in n.targets))
        attempts = next(n for n in ast.walk(population) if isinstance(n, ast.For)
                        and ast.unparse(n.target) == "_attempt")
        self.assertLess(flag.lineno, attempts.lineno)
        self.assertEqual(ast.unparse(flag.value.func), "self._meli_is_pack_route")


class RecentPollingSafetyTests(unittest.TestCase):
    class SerializationFailure(Exception):
        """Catch-routing double only; native tests use psycopg2's real class."""

    def assert_recent_group_propagation(self, error_type, kind="recent"):
        bodies = [payload(), payload(102, 3, 125)]
        client = MutableClient(bodies)
        client.routes["/orders/search?seller=42&sort=date_desc"] = (200, {
            "results": bodies, "paging": {"total": 2, "limit": 2}})
        company = SimpleNamespace(id=1, _fields={})
        class Environment(dict):
            context = {}
            user = SimpleNamespace(company_id=company)
        env = Environment()
        caller = {"sentinel": "uncommitted caller work"}
        projected, attempts, rollbacks, commits = [], [], [], []
        failure = error_type("late second-member failure through public recent")
        class OrderProbe(SimpleNamespace):
            def __iter__(self):
                yield self
        probe = OrderProbe(env=env, id=1, order_id="101", seller=repr({"id": 42}),
            pack_id="900", shipping_id=False, sale_order=False,
            _meli_assert_order_admission_actor=lambda config: "42",
            _meli_preflight_order_history=lambda *args, **kw: None)
        env["mercadolibre.orders"] = probe
        env["meli.warning"] = SimpleNamespace()
        env["sale.order"] = SimpleNamespace()
        env["meli.util"] = SimpleNamespace(get_new_instance=lambda company: client)
        probe.browse = lambda oid: probe
        scope = {"Autocommit": lambda *args: None, "ast": ast, "AccessError": ValueError,
                 "MeliRollback": lambda *args: rollbacks.append("attempt"),
                 "MeliCommit": lambda *args: commits.append("attempt"),
                 "psycopg2_errors": SimpleNamespace(SerializationFailure=self.SerializationFailure),
                 "_logger": SimpleNamespace(info=lambda *args: None,
                     error=lambda *args, **kw: None, warning=lambda *args: None)}
        for name, prefix in (("_meli_is_pack_route", False), ("orders_query_iterate", False),
                             ("orders_update_order", False), ("orders_update_order_json", True)):
            setattr(probe, name, MethodType(method(name, prefix=prefix, extra_scope=scope), probe))
        def project_group(current, config, meli):
            # Projection/savepoint double only; registered native tests exercise ORM.
            attempts.append(current["id"])
            before = list(projected)
            try:
                for body in bodies:
                    projected.append(body["id"])
                    if body["id"] == bodies[1]["id"]:
                        raise failure
            finally:
                projected[:] = before
        probe._meli_import_pack = project_group
        caught = None
        try:
            if kind == "recent":
                method("orders_query_recent", extra_scope=scope)(probe, meli=client, config=company)
            else:
                env.context = {"active_model": "mercadolibre.orders", "active_ids": [probe.id]}
                method("order_update", extra_scope=scope)(SimpleNamespace(env=env))
        except error_type as error:
            caught = error
        self.assertEqual(attempts, [101], "No fallback, second member replay, or nested retry")
        self.assertEqual(client.calls, ["/orders/search?seller=42&sort=date_desc"]
                         if kind == "recent" else ["/orders/101"])
        self.assertEqual(projected, [])
        self.assertEqual(caller, {"sentinel": "uncommitted caller work"})
        self.assertEqual((rollbacks, commits), ([], []), "GLOBAL_TRANSACTION_ATTEMPT")
        self.assertIs(caught, failure, "Public polling must preserve the original exception object")

    def test_public_recent_preserves_late_group_runtime_failure(self):
        self.assert_recent_group_propagation(RuntimeError)

    def test_public_recent_preserves_late_group_serialization_failure_without_retry(self):
        self.assert_recent_group_propagation(self.SerializationFailure)

    def test_actual_update_wizard_preserves_late_group_runtime_failure(self):
        self.assert_recent_group_propagation(RuntimeError, kind="wizard")

    def test_actual_update_wizard_preserves_late_group_serialization_failure(self):
        self.assert_recent_group_propagation(self.SerializationFailure, kind="wizard")

    def test_actual_update_wizard_preserves_individual_legacy_error_handling(self):
        rollbacks = []
        failure = RuntimeError("individual legacy update error")
        def fail():
            raise failure
        order = SimpleNamespace(orders_update_order=fail, _meli_is_pack_route=lambda *args: False)
        env = {"meli.warning": object(), "sale.order": object(),
               "mercadolibre.orders": SimpleNamespace(browse=lambda oid: order)}
        class Environment(dict):
            context = {"active_model": "mercadolibre.orders", "active_ids": [1]}
        scope = {"Autocommit": lambda *args: None,
                 "MeliRollback": lambda *args: rollbacks.append("attempt"),
                 "_logger": SimpleNamespace(info=lambda *args: None, error=lambda *args, **kw: None)}
        self.assertEqual(method("order_update", extra_scope=scope)(
            SimpleNamespace(env=Environment(env))), {})
        self.assertEqual(rollbacks, ["attempt"])

    def test_public_recent_preserves_individual_legacy_error_handling(self):
        company = SimpleNamespace(id=1, _fields={})
        failure, rollbacks = RuntimeError("individual legacy error"), []
        def fail(**kwargs):
            raise failure
        probe = SimpleNamespace(env=SimpleNamespace(user=SimpleNamespace(company_id=company)),
                                orders_query_iterate=fail)
        scope = {"Autocommit": lambda *args: None,
                 "MeliRollback": lambda *args: rollbacks.append("attempt"),
                 "_logger": SimpleNamespace(info=lambda *args: None, error=lambda *args, **kw: None)}
        self.assertEqual(method("orders_query_recent", extra_scope=scope)(
            probe, meli=object(), config=company), {})
        self.assertEqual(rollbacks, ["attempt"])


if __name__ == "__main__":
    unittest.main()
