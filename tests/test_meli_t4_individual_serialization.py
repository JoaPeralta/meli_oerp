"""Source-only existing-parent serialization/abort transport contracts.

Compile complete current methods, including the installed bridge wrapper MRO.
Only ORM/SQL/transport boundaries are doubles. No Odoo, PostgreSQL, SDK or network
is imported or executed, and these tests cannot prove physical tuple changes.
An explicit verified bridge binding is required; no historical sibling fallback.
"""
import ast
import copy
import datetime
from decimal import Decimal, InvalidOperation
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from contextlib import contextmanager
from functools import wraps
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout.decode().strip()


def verified_bridge():
    names = ("MELI_T4D_BRIDGE_ROOT", "MELI_T4D_BRIDGE_CANONICAL_ROOT")
    values = [os.environ.get(name) for name in names]
    if not all(values):
        raise RuntimeError("Explicit bridge root and canonical root bindings are required")
    bridge, canonical = [Path(value).resolve(strict=True) for value in values]
    if Path(git(bridge, "rev-parse", "--show-toplevel")).resolve() != bridge:
        raise RuntimeError("Bridge binding is not its actual Git top-level")
    common = lambda root: Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if common(bridge) != common(canonical):
        raise RuntimeError("Bridge binding is not linked to the selected canonical repository")
    if not (bridge / "addons/viarengo_circuit_meli/models/cancellation.py").is_file():
        raise RuntimeError("Bound bridge has no actual installed-wrapper source")
    return bridge


BRIDGE = verified_bridge()
spec = importlib.util.spec_from_file_location("t4d_source_admission", ROOT / "models/meli_pack_admission.py")
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)


class Denied(Exception):
    pass


class SerializationFailure(Exception):
    pgcode = "40001"


class DeadlockDetected(Exception):
    pgcode = "40P01"


class UniqueViolation(Exception):
    pgcode = "23505"


class GlobalRollbackBlocked(Exception):
    """A recorded forbidden attempt, not real cursor.rollback or SQL erasure."""


class WriteBoundaryReached(Exception):
    pass


class SQL(str):
    @staticmethod
    def Identifier(value):
        return SQL(value)


class Rows(list):
    def __getitem__(self, key):
        return getattr(self, key) if isinstance(key, str) else super().__getitem__(key)

    @property
    def ids(self):
        return [row.id for row in self]

    def __getattr__(self, name):
        if len(self) == 1:
            return getattr(self[0], name)
        raise AttributeError(name)

    def __or__(self, other):
        other = other if isinstance(other, (list, tuple)) else [other]
        return Rows(list(self) + [row for row in other if not any(row is old for old in self)])

    def ensure_one(self):
        assert len(self) == 1

    def exists(self):
        return self

    def filtered(self, predicate):
        return Rows(row for row in self if predicate(row))

    def mapped(self, name):
        return Rows(value for row in self if (value := getattr(row, name)))

    def check_access(self, mode):
        for row in self:
            row.check_access(mode)

    def flush_recordset(self, fields):
        return None


class Record(SimpleNamespace):
    def __getitem__(self, key):
        return getattr(self, key)


class Cursor:
    def __init__(self, fixture):
        self.fixture, self.calls, self.rowcount = fixture, [], 1

    def execute(self, query, params=None):
        self.calls.append((str(query), copy.deepcopy(params)))

    def fetchone(self):
        if self.calls and self.calls[-1][0].lstrip().startswith("UPDATE mercadolibre_orders"):
            return (12,)
        return (False,)

    def fetchall(self):
        return [(90,)]  # Existing buyer avoids any create/race branch.

    @contextmanager
    def savepoint(self):
        before = copy.deepcopy(self.fixture.caller)
        try:
            yield
        except BaseException:
            self.fixture.caller.clear()
            self.fixture.caller.update(before)
            raise


def compile_function(path, name, scope):
    node = next(node for node in ast.walk(ast.parse(path.read_bytes()))
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = [decorator for decorator in node.decorator_list
                           if isinstance(decorator, ast.Name)
                           and decorator.id == "_meli_order_import_savepoint"]
    exec(compile(ast.fix_missing_locations(ast.Module([node], [])), str(path), "exec"), scope)
    return scope[name]


def compile_layer(path, name, base, methods, scope):
    node = copy.deepcopy(next(node for node in ast.parse(path.read_bytes()).body
                             if isinstance(node, ast.ClassDef) and node.name == name))
    node.bases, node.keywords, node.decorator_list = [ast.Name(id="_source_base", ctx=ast.Load())], [], []
    node.body = [method for method in node.body
                 if isinstance(method, ast.FunctionDef) and method.name in methods]
    assert {method.name for method in node.body} == set(methods)
    for method in node.body:
        method.decorator_list = []
    scope["_source_base"] = base
    exec(compile(ast.fix_missing_locations(ast.Module([node], [])), str(path), "exec"), scope)
    return scope[name]


class Fixture:
    """Valid existing OWN/A individual identity, stopped at first existing write."""
    def __init__(self, failure):
        self.failure = failure
        self.caller = {"sentinel": "unrelated uncommitted caller work"}
        self.access, self.writes, self.global_rollbacks, self.commits = [], [], [], []
        self.company = SimpleNamespace(id=1, _fields={}, partner_id=SimpleNamespace(lang="en_US"),
            mercadolibre_pricelist=object(), name="Synthetic company", mercadolibre_seller_id="42")
        self.user = SimpleNamespace(id=5, active=True, share=False, company_id=self.company)
        self.company.mercadolibre_seller_user = self.user
        self.body = {"id": 101, "seller": {"id": 42}, "status": "paid", "currency_id": "USD",
            "tags": [], "shipping": None, "payments": [], "total_amount": 200, "paid_amount": 200,
            "buyer": {"id": 50, "name": "Synthetic buyer", "first_name": "Synthetic", "last_name": "Buyer"},
            "order_items": [{"item": {"id": "MLA123", "variation_id": 7},
                             "quantity": 2, "unit_price": 100, "currency_id": "USD"}]}
        root = SimpleNamespace(id=20, company_id=self.company, viarengo_circuit_code="a",
            state="draft", picking_ids=[], invoice_ids=[], meli_update_forbidden=False,
            meli_order_id="101", meli_status_detail="", order_line=[],
            _fields={"meli_orders": SimpleNamespace(relation="source_double_rel",
                                                     column1="sale_id", column2="order_id")},
            check_access=lambda mode: self.access.append(("root", mode)))
        self.root = Rows([root])
        self.cr = Cursor(self)
        class Environment(dict):
            pass
        self.env = Environment()
        self.env.cr, self.env.uid, self.env.user, self.env.su = self.cr, 5, self.user, False
        self.env.company, self.env.companies = self.company, self.company
        self.env.context = {"viarengo_meli_root_circuit": "a"}
        self.client_calls = []
        fixture = self
        class Client:
            access_token = None
            needlogin_state = False
            status_code = last_status_code = 200
            def get(self, path, params):
                fixture.client_calls.append(path)
                self.path = path
                return self
            def json(self):
                if self.path.startswith("/orders/search"):
                    return {"results": [copy.deepcopy(fixture.body)], "paging": {"total": 1, "limit": 1}}
                return copy.deepcopy(fixture.body)
        self.client = Client()
        logger = SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None,
                                 warning=lambda *a, **k: None)
        def blocked_rollback(*args):
            self.global_rollbacks.append("MeliRollback attempted and blocked")
            raise GlobalRollbackBlocked("Caller-global rollback attempt blocked by source fence")
        self.scope = {"ast": ast, "wraps": wraps, "ValidationError": Denied, "AccessError": Denied,
            "AdmissionError": admission.AdmissionError, "remote_id": admission.remote_id,
            "verify_order": admission.verify_order, "snapshot_response": admission.snapshot_response,
            "MeliCr": lambda record: record.env.cr, "sql": SimpleNamespace(SQL=SQL, Identifier=SQL.Identifier),
            "_logger": logger, "Autocommit": lambda *args: None,
            "MeliCommit": lambda *args: self.commits.append("MeliCommit attempted"),
            "MeliRollback": blocked_rollback, "CANCELLATION_STATES": ("cancelled", "pending_cancel", "partially_refunded"),
            "psycopg2_errors": SimpleNamespace(SerializationFailure=SerializationFailure),
            "fields": SimpleNamespace(Datetime=SimpleNamespace(now=datetime.datetime.now)),
            "timedelta": datetime.timedelta, "DECLARATION": "viarengo_meli_root_circuit"}
        self.scope.update(Decimal=Decimal, InvalidOperation=InvalidOperation, json=json)
        for name in ("amount", "cancellation_evidence"):
            compile_function(ROOT / "models/meli_cancellation.py", name, self.scope)
        orders_path = ROOT / "models/orders.py"
        compile_function(orders_path, "_meli_order_import_savepoint", self.scope)
        compile_function(BRIDGE / "addons/viarengo_circuit_meli/policy.py", "root_values", self.scope)
        generic = type("CurrentGenericOrders", (SimpleNamespace,), {})
        names = ("_meli_assert_order_admission_actor", "_meli_preflight_order_history",
            "_meli_assert_individual_sale_root", "_meli_order_admission", "_meli_is_pack_route",
            "orders_update_order_json", "orders_query_iterate", "orders_query_recent", "orders_resync_status")
        for name in names:
            setattr(generic, name, compile_function(orders_path, name, self.scope))
        bridge_admission = compile_layer(BRIDGE / "addons/viarengo_circuit_meli/models/pack_admission.py",
            "MeliOrderAdmission", generic, ["_meli_assert_order_admission_actor"], self.scope)
        installed = compile_layer(BRIDGE / "addons/viarengo_circuit_meli/models/cancellation.py",
            "SourceCancellation", bridge_admission,
            ["_meli_observation_anchor", "_meli_cancel_snapshot", "orders_update_order"], self.scope)
        registration = (BRIDGE / "addons/viarengo_circuit_meli/models/__init__.py").read_text(encoding="utf-8")
        if "from . import individual_serialization" in registration.splitlines():
            path = BRIDGE / "addons/viarengo_circuit_meli/models/individual_serialization.py"
            # Only a real registered class participates; BASE still executes its
            # existing public JSON, never a missing proposed helper as RED.
            for name in ("membership_incomplete", "line_population_incomplete"):
                compile_function(BRIDGE / "addons/viarengo_circuit_meli/models/cancellation.py", name, self.scope)
            self.scope["SOURCE"] = "meli_source_order_item_id"
            methods = [node.name for node in next(node for node in ast.parse(path.read_bytes()).body
                if isinstance(node, ast.ClassDef) and node.name == "IndividualSerialization").body
                if isinstance(node, ast.FunctionDef)]
            installed = compile_layer(path, "IndividualSerialization", installed, methods, self.scope)
        # The bridge's native configuration/group/ACL query is a boundary double;
        # generic actor and current bridge admission/wrapper bodies execute below.
        def actor():
            if self.env.uid != 5 or self.env.su or self.env.company is not self.company:
                raise Denied("Bridge actor boundary double denies invalid environment")
            self.access.append(("bridge_actor", "validated double"))
            return self.company
        self.env["viarengo.meli.bridge"] = SimpleNamespace(_assert_actor=actor)
        self.model = installed(env=self.env, id=12, ids=[12], order_id="101", seller=repr({"id": 42}),
            company_id=self.company, seller_id=self.user, sale_order=self.root, pack_id=False,
            shipment=False, currency_id="USD", status="paid", status_detail="", _fields={})
        installed.__iter__ = lambda record: iter([record])
        installed.__len__ = lambda record: 1
        installed.__or__ = lambda record, other: record
        installed.ensure_one = lambda record: None
        installed.exists = lambda record: record
        installed.search = lambda record, *args, **kwargs: record
        installed.browse = lambda record, *args: record if args and args[0] == 12 else Rows()
        installed.check_access = lambda record, mode: self.access.append(("order", mode))
        installed.flush_model = lambda *args: None
        installed.flush_recordset = lambda *args: None
        root.meli_orders = Rows([self.model])
        root.env = self.env
        source = SimpleNamespace(id=30, order_id=self.model, product_id=SimpleNamespace(id=2),
            order_item_id="MLA123", order_item_variation_id="7", quantity=2, unit_price="100",
            check_access=lambda mode: self.access.append(("source", mode)))
        root.order_line = Rows([SimpleNamespace(id=40, order_id=self.root, company_id=self.company,
            display_type=False, product_id=source.product_id, meli_source_order_item_id=source,
            check_access=lambda mode: self.access.append(("line", mode)))])
        currency = SimpleNamespace(name="USD", compare_amounts=lambda a, b: (a > b) - (a < b))
        root.currency_id, root.amount_total, root.locked = currency, 200, False
        source.product_id.uom_id = SimpleNamespace(id=1)
        source.product_id.company_id, source.currency_id = self.company, "USD"
        line = Record(**vars(root.order_line[0]))
        root.order_line = Rows([line])
        line.product_uom_id, line.currency_id = source.product_id.uom_id, currency
        line.meli_order_item_id, line.meli_order_item_variation_id = "MLA123", "7"
        line.product_uom_qty, line.price_unit, line.price_total = 2, 100, 200
        line.price_subtotal, line.price_tax, line.discount = 200, 0, 0
        line.is_delivery, line.tax_ids, line.move_ids = False, Rows(), Rows()
        line.qty_delivered = line.qty_invoiced = 0
        self.model.total_amount = self.model.paid_amount = 200
        self.env["mercadolibre.orders"] = self.model
        self.env["sale.order"] = SimpleNamespace(search=lambda *args, **kwargs: self.root)
        plain = SimpleNamespace(_fields={}, flush_model=lambda fields: None)
        for name in ("product.product", "product.pricelist", "res.partner", "mercadolibre.posting",
                     "mercadolibre.payments", "mercadolibre.shipment"):
            self.env[name] = plain
        self.env["sale.order.line"] = SimpleNamespace(_fields={"meli_source_order_item_id": True},
                                                       flush_model=lambda fields: None)
        self.env["mercadolibre.order_items"] = plain
        self.env["meli.util"] = SimpleNamespace(get_new_instance=lambda config: self.client)
        self.env["meli.warning"] = SimpleNamespace()
        class Buyer:
            id = 90
            def sudo(record):
                return record  # Existing legacy source calls this; no real elevation.
            def browse(record, ids):
                return record
            def write(record, values):
                self.writes.append(copy.deepcopy(values))
                raise self.failure
        self.env["mercadolibre.buyers"] = Buyer()
        self.model.prepare_ml_order_vals = lambda **kwargs: {"context": ""}
        self.model.get_billing_info = lambda **kwargs: {}
        self.model.buyer_full_name = lambda buyer: "Synthetic Buyer"
        self.model.fix_locals = lambda **kwargs: {}
        self.model.billing_info = lambda value: repr(value)
        self.model.buyer_additional_info = lambda value: {}
        for name in ("street", "city", "country", "state", "zip_code", "full_phone", "full_alt_phone"):
            setattr(self.model, name, lambda *args: "")
        self.wizard = compile_function(orders_path, "order_update", self.scope)

    def invoke(self, route):
        if route == "json":
            return self.model.orders_update_order_json(
                {"id": 12, "remote_order_id": "101", "order_json": copy.deepcopy(self.body)},
                config=self.company, meli=self.client)
        if route == "wrapper":
            return self.model.orders_update_order(config=self.company, meli=self.client)
        if route == "polling":
            return self.model.orders_query_iterate(config=self.company, meli=self.client, fetch_ids=[])
        if route == "recent":
            return self.model.orders_query_recent(config=self.company, meli=self.client)
        if route == "wizard":
            self.env.context.update(active_model="mercadolibre.orders", active_ids=[12])
            return self.wizard(SimpleNamespace(env=self.env))
        if route == "resync":
            self.model.status = "confirmed"  # Legacy selector needs a real status change.
            return self.model.orders_resync_status(config=self.company, meli=self.client)
        raise AssertionError("Unknown source contract route")


class ResyncFixture(Fixture):
    """Actual resync/registered guard; only commercial ORM producer is doubled.

    Source checks establish routing/order/output checks, not native SQL rollback,
    canonical mail validation or Odoo economics. Registered native tests cover those.
    """
    def __init__(self):
        super().__init__(RuntimeError("Unexpected legacy buyer boundary"))
        self.body["pack_id"] = None
        self.model._name = "mercadolibre.orders"
        currency = SimpleNamespace(name="USD", compare_amounts=lambda a, b: (a > b) - (a < b))
        root = self.root[0]
        line = Record(**vars(self.root.order_line[0]))
        root.order_line = Rows([line])
        self.source = line.meli_source_order_item_id
        uom = SimpleNamespace(id=1)
        self.source.product_id.uom_id = uom
        self.source.product_id.company_id = self.company
        self.source.currency_id = "USD"
        root.currency_id, root.amount_total, root.locked = currency, 200, False
        line.product_uom_id, line.currency_id = uom, currency
        line.meli_order_item_id, line.meli_order_item_variation_id = "MLA123", "7"
        line.product_uom_qty, line.price_unit, line.price_total = 2, 100, 200
        line.price_subtotal, line.price_tax, line.discount = 200, 0, 0
        line.is_delivery, line.tax_ids, line.move_ids = False, Rows(), Rows()
        line.qty_delivered = line.qty_invoiced = 0
        self.model.total_amount = self.model.paid_amount = 200
        self.line, self.notes, self.projections = line, [], []
        self.factory_calls = []
        self.env.flush_all = lambda: None
        self.env["meli.util"].get_new_instance = lambda config: self.factory_calls.append(config) or self.client
        self.scope.update(Decimal=Decimal, InvalidOperation=InvalidOperation, json=json)
        for name in ("amount", "cancellation_evidence"):
            compile_function(ROOT / "models/meli_cancellation.py", name, self.scope)
        compile_function(BRIDGE / "addons/viarengo_circuit_meli/models/cancellation.py",
                         "_meli_cancel_snapshot", self.scope)
        self.model._meli_cancel_snapshot = lambda members, lines: self.scope["_meli_cancel_snapshot"](
            self.model, members, lines)
        self.model._meli_cancel_population = lambda config: Rows([self.model])
        self.model._meli_audit_document = lambda note, target: note.document
        self.model._meli_replay_postconditions = lambda *args: not self.line.product_uom_qty and root.state == "cancel"
        self.env["mail.message"] = SimpleNamespace(search=lambda *args: Rows(self.notes))
        root.meli_cancel_with_detail = lambda *args: self.projections.append("FORBIDDEN_LEGACY_WHOLE_CANCEL")
        self.members = [self]
        self.after_projection = lambda fixture: None
        self.producer_result = {}
        def search(domain, **kwargs):
            matches = Rows(f.model for f in self.members
                if not any(term[:2] == ("order_id", "=") and str(term[2]) != f.model.order_id
                           for term in domain))
            # Odoo singleton recordsets retain model identity, not Python list identity.
            return matches[0] if len(matches) == 1 and any(term[0] == "order_id" for term in domain) else matches
        self.model.search = search
        self.model.browse = lambda local_id=None: next((f.model for f in self.members if local_id == f.model.id), Rows())
        self.env["mail.message"].search = lambda *args: Rows(note for f in self.members for note in f.notes)
        old_fetch = self.cr.fetchone
        def fetch():
            query, params = self.cr.calls[-1]
            if query.startswith("UPDATE mercadolibre_orders"):
                return (params[0],)
            if "meli_source_order_item_id IS NOT NULL" in query:
                return (bool(self.line.meli_source_order_item_id),)
            return old_fetch()
        self.cr.fetchone = fetch
        @contextmanager
        def savepoint():
            states = [(f, f.state(), list(f.notes)) for f in self.members]
            caller = copy.deepcopy(self.caller)
            try:
                yield
            except BaseException:
                for f, state, notes in states:
                    f.restore(state)
                    f.notes[:] = notes
                self.caller.clear()
                self.caller.update(caller)
                raise
        self.cr.savepoint = savepoint
        generic = next(cls for cls in type(self.model).__mro__ if cls.__name__ == "CurrentGenericOrders")
        def project(model, data, **kwargs):
            selected = next(f for f in self.members if f.model.id == data["id"])
            model, root, line = selected.model, selected.root[0], selected.line
            body = data["order_json"]
            selected.projections.append(copy.deepcopy(body))
            before = model._meli_cancel_snapshot(Rows([model]), model.sale_order.order_line)
            if body["status"] in self.scope["CANCELLATION_STATES"]:
                reason = "processed_history_requires_reconciliation" if root.state == "sale" else (
                    "remote_cancellation_not_terminal" if body["status"] != "cancelled" else (
                    "split_or_transformation_requires_reconciliation" if "split" in body.get("tags", []) else False))
                model.status, model.status_detail = body["status"], body.get("status_detail") or ""
                if not reason:
                    line.product_uom_qty, line.price_total, line.price_subtotal = 0, 0, 0
                    root.amount_total, root.state = 0, "cancel"
                result = {"cancellation": "reconciliation" if reason else "applied", "error": reason,
                          "order_id": model.order_id, "sale_order_id": root.id}
                evidence = self.scope["cancellation_evidence"](body, "42", model.order_id)
                selected.notes.append(SimpleNamespace(document={"evidence": evidence,
                    "outcome": reason or "applied", "before": before,
                    "after": model._meli_cancel_snapshot(Rows([model]), model.sale_order.order_line)}))
                return result
            item = body["order_items"][0]
            selected.source.quantity, selected.source.unit_price = item["quantity"], str(item["unit_price"])
            line.product_uom_qty, line.price_unit = item["quantity"], item["unit_price"]
            line.price_total = line.price_subtotal = root.amount_total = item["quantity"] * item["unit_price"]
            model.status, model.total_amount, model.paid_amount = "paid", body["total_amount"], body["paid_amount"]
            self.after_projection(selected)
            return self.producer_result  # Actual scalar producer's normal contract is {}.
        generic.orders_update_order_json = project

    def state(self):
        return (self.model.status, self.model.status_detail, self.model.total_amount, self.model.paid_amount,
            self.source.quantity, self.source.unit_price, self.line.product_uom_qty,
            self.line.price_unit, self.line.price_total, self.line.price_subtotal,
            self.root.state, self.root.amount_total)

    def restore(self, value):
        (self.model.status, self.model.status_detail, self.model.total_amount, self.model.paid_amount,
            self.source.quantity, self.source.unit_price, self.line.product_uom_qty,
            self.line.price_unit, self.line.price_total, self.line.price_subtotal,
            self.root[0].state, self.root[0].amount_total) = value

    def resync(self, client=True):
        return self.model.orders_resync_status(config=self.company, meli=self.client if client else None)

    def add_second(self):
        other = ResyncFixture()
        other.model.id, other.model.ids, other.model.order_id = 13, [13], "102"
        other.root[0].id, other.root[0].meli_order_id = 21, "102"
        other.source.id, other.line.id = 31, 41
        other.model.env, other.root[0].env = self.env, self.env
        other.model.company_id, other.model.seller_id = self.company, self.user
        other.root[0].company_id = other.line.company_id = other.source.product_id.company_id = self.company
        self.members.append(other)
        return other


class IndividualResyncTests(unittest.TestCase):
    def test_same_status_paid_projects_fresh_economics_once_via_registered_guard(self):
        fixture = ResyncFixture()
        fixture.body["order_items"][0].update(quantity=4, unit_price=125)
        fixture.body.update(total_amount=500, paid_amount=500)
        result = fixture.resync()
        self.assertEqual(len(fixture.projections), 1, "SAME_STATUS_UPDATE_WAS_SKIPPED")
        self.assertEqual((fixture.source.quantity, fixture.line.product_uom_qty, fixture.root.amount_total), (4, 4, 500))
        self.assertEqual(fixture.client_calls, ["/orders/101"])
        self.assertEqual(result["results"][0]["outcome"], "verified")
        self.assertEqual((fixture.commits, fixture.global_rollbacks), ([], []))
        self.assertEqual(sum(q.startswith("UPDATE mercadolibre_orders") for q, _ in fixture.cr.calls), 1)

    def test_confirmed_cancel_uses_c5_not_legacy_status_prewrite_or_whole_cancel(self):
        fixture = ResyncFixture()
        fixture.body["status"] = "cancelled"
        fixture.resync()
        self.assertNotIn("FORBIDDEN_LEGACY_WHOLE_CANCEL", fixture.projections)
        self.assertEqual((fixture.source.quantity, fixture.line.product_uom_qty, fixture.model.paid_amount), (2, 0, 200))
        self.assertEqual((fixture.root.state, len(fixture.notes)), ("cancel", 1))
        self.assertEqual((fixture.commits, fixture.global_rollbacks), ([], []))

    def test_processed_and_nonterminal_observations_are_audited_reconciliation(self):
        for status, processed, tags in (("cancelled", True, []), ("pending_cancel", False, []),
                                        ("partially_refunded", False, []), ("cancelled", False, ["split"])):
            with self.subTest(status=status, processed=processed, tags=tags):
                fixture = ResyncFixture()
                fixture.body.update(status=status, tags=tags)
                if processed:
                    fixture.root[0].state = "sale"
                result = fixture.resync()
                self.assertEqual(len(fixture.projections), 1, "OBSERVATION_DID_NOT_REACH_C5")
                self.assertEqual(result.get("scope"), "known_own_individual_resync",
                                 "LEGACY_RESULT_IS_NOT_C5_RECONCILIATION")
                self.assertEqual(result["results"][0]["outcome"], "reconciliation")
                self.assertEqual((fixture.source.quantity, fixture.line.product_uom_qty, fixture.root.amount_total), (2, 2, 200))
                self.assertEqual(len(fixture.notes), 1)
                self.assertEqual((fixture.commits, fixture.global_rollbacks), ([], []))

    def test_preflight_denies_actor_company_hidden_or_ambiguous_graph_before_factory(self):
        for kind in ("sudo", "b", "foreign", "hidden", "duplicate_root", "identity"):
            with self.subTest(kind=kind):
                fixture = ResyncFixture()
                if kind == "sudo":
                    fixture.env.su = True
                elif kind == "b":
                    fixture.root[0].viarengo_circuit_code = "b"
                elif kind == "foreign":
                    fixture.model.seller = repr({"id": 99})
                elif kind == "identity":
                    fixture.line.meli_order_item_id = "MLA999"
                else:
                    old = fixture.cr.fetchone
                    match = "UNION ALL SELECT 1 FROM mercadolibre_order_items" if kind == "hidden" else "FROM sale_order WHERE meli_order_id=%s"
                    fixture.cr.fetchone = lambda old=old, match=match: (True,) if match in fixture.cr.calls[-1][0] else old()
                with self.assertRaises(Denied):
                    fixture.resync(client=False)
                self.assertEqual((fixture.factory_calls, fixture.client_calls, fixture.projections), ([], [], []))
                self.assertEqual((fixture.commits, fixture.global_rollbacks), ([], []))

    def test_all_selected_graphs_preflight_before_factory_including_mixed_unbound(self):
        fixture = ResyncFixture()
        second = fixture.add_second()
        second.line.meli_source_order_item_id = False
        # The physical binding Boolean is false for the unbound second root.
        old = fixture.cr.fetchone
        fixture.cr.fetchone = lambda: (False,) if "meli_source_order_item_id IS NOT NULL" in fixture.cr.calls[-1][0] else old()
        with self.assertRaises(Denied):
            fixture.resync(client=False)
        self.assertEqual((fixture.factory_calls, fixture.client_calls, fixture.projections), ([], [], []))

    def test_zero_bound_population_retains_explicit_legacy_control(self):
        fixture = ResyncFixture()
        fixture.line.meli_source_order_item_id = False
        result = fixture.resync()
        self.assertNotIn("scope", result)
        self.assertEqual(result, {"checked": 1, "changed": 0, "cancelled": 0})
        self.assertEqual(fixture.projections, [])

    def test_cold_anonymous_mapping_cannot_overwrite_unconsumed_binding_sql(self):
        fixture = ResyncFixture()
        fixture.line.meli_source_order_item_id = False
        events, pending = [], []
        actual_execute = fixture.cr.execute
        def execute(query, params=None):
            actual_execute(query, params)
            binding = "meli_source_order_item_id IS NOT NULL" in str(query)
            events.append("binding SELECT" if binding else "ORM SELECT")
            pending[:] = [(False,)] if binding else [(fixture.line.id, None)]
        def fetchone():
            events.append("fetchone binding" if pending == [(False,)] else "fetchone exhausted")
            return pending.pop(0) if pending else None
        def fetchall():
            events.append("fetchall ORM")
            rows = list(pending)
            pending.clear()
            return rows
        class ColdAnonymousRows(Rows):
            def mapped(records, name):
                if name == "meli_source_order_item_id":
                    fixture.cr.execute("SELECT id,meli_source_order_item_id FROM sale_order_line WHERE id=%s",
                                       [fixture.line.id])
                    fixture.cr.fetchall()  # A realistic ORM fetch consumes this SAME cursor.
                return super().mapped(name)
        fixture.root[0].order_line = ColdAnonymousRows([fixture.line])
        fixture.cr.execute, fixture.cr.fetchone, fixture.cr.fetchall = execute, fetchone, fetchall
        try:
            result = fixture.model._meli_preflight_individual_resync(
                Rows([fixture.model]), fixture.company, None)
        except TypeError as error:
            self.fail("BINDING_RESULT_OVERWRITTEN_BY_COLD_ORM_FETCH: " + repr(error) + " events=" + repr(events))
        self.assertIs(result, False, "An authorized anonymous-only population retains the legacy control")
        self.assertLess(events.index("fetchone binding"), events.index("ORM SELECT"))
        self.assertEqual((fixture.factory_calls, fixture.client_calls, fixture.projections), ([], [], []))

    def test_partial_or_missing_http_outcome_is_pending_without_business_writes(self):
        for status in (206, None, 500):
            with self.subTest(status=status):
                fixture = ResyncFixture()
                fixture.client.status_code = fixture.client.last_status_code = status
                before = fixture.state()
                result = fixture.resync()
                self.assertEqual(result["results"][0]["outcome"], "pending")
                self.assertEqual(fixture.state(), before)
                self.assertEqual(fixture.projections, [])

    def test_fresh_foreign_pack_currency_or_incomplete_payload_refuses_without_projection(self):
        for kind in ("foreign", "pack", "currency", "items"):
            with self.subTest(kind=kind):
                fixture = ResyncFixture()
                if kind == "foreign":
                    fixture.body["seller"]["id"] = 99
                elif kind == "pack":
                    fixture.body["pack_id"] = 900
                elif kind == "currency":
                    fixture.body["currency_id"] = "ARS"
                else:
                    fixture.body["order_items"] = []
                before = fixture.state()
                with self.assertRaises(Denied):
                    fixture.resync()
                self.assertEqual(fixture.state(), before)
                self.assertEqual((fixture.projections, fixture.global_rollbacks), ([], []))

    def test_false_or_unprojected_scalar_result_rolls_back_not_inferred_success(self):
        for value in (False, None, {"error": "synthetic failure"}, {}):
            with self.subTest(result=value):
                fixture = ResyncFixture()
                fixture.body["order_items"][0].update(quantity=4, unit_price=125)
                fixture.body.update(total_amount=500, paid_amount=500)
                fixture.producer_result = value
                if value == {}:
                    fixture.after_projection = lambda selected: setattr(selected.line, "product_uom_qty", 2)
                before = fixture.state()
                with self.assertRaises(Denied):
                    fixture.resync()
                self.assertEqual(fixture.state(), before)
                self.assertEqual((fixture.commits, fixture.global_rollbacks), ([], []))

    def assert_batch_original_failure(self, error_type):
        fixture = ResyncFixture()
        second = fixture.add_second()
        original = error_type("Second already-owned individual late failure")
        fixture.body["order_items"][0].update(quantity=4, unit_price=125)
        fixture.body.update(total_amount=500, paid_amount=500)
        base_get = fixture.client.get
        def get(path, params):
            result = base_get(path, params)
            fixture.body["id"] = 102 if path == "/orders/102" else 101
            return result
        fixture.client.get = get
        def late(selected):
            if selected is second:
                self.assertEqual(fixture.root.amount_total, 500, "FIRST_ROOT_NOT_MUTATED_BEFORE_LATE_FAILURE")
                raise original
        fixture.after_projection = late
        before = [f.state() for f in fixture.members]
        with self.assertRaises(error_type) as caught:
            fixture.resync()
        self.assertIs(caught.exception, original)
        self.assertTrue(original._meli_individual_operation_failure)
        self.assertEqual([f.state() for f in fixture.members], before)
        self.assertEqual(fixture.caller, {"sentinel": "unrelated uncommitted caller work"})
        self.assertEqual((fixture.commits, fixture.global_rollbacks), ([], []))
        self.assertEqual(fixture.client_calls, ["/orders/101", "/orders/102"])

    def test_second_root_runtime_preserves_original_and_whole_owned_batch(self):
        self.assert_batch_original_failure(RuntimeError)

    def test_second_root_serialization_preserves_original_and_whole_owned_batch(self):
        self.assert_batch_original_failure(SerializationFailure)


class IndividualSerializationTests(unittest.TestCase):
    def updates(self, fixture):
        return [(query, params) for query, params in fixture.cr.calls
                if query.lstrip().startswith("UPDATE mercadolibre_orders")]

    def assert_denied_before_physical_write(self, fixture, data=None):
        data = data or {"id": 12, "remote_order_id": "101", "order_json": copy.deepcopy(fixture.body)}
        with self.assertRaises(Denied):
            fixture.model.orders_update_order_json(data, config=fixture.company, meli=fixture.client)
        self.assertEqual((self.updates(fixture), fixture.writes, fixture.global_rollbacks), ([], [], []))
        self.assertEqual(fixture.caller, {"sentinel": "unrelated uncommitted caller work"})

    def assert_original_transport(self, route, error_type):
        original = error_type("late failure at actual existing buyer.write boundary")
        fixture = Fixture(original)
        caught = None
        try:
            fixture.invoke(route)
        except Exception as error:
            caught = error
        self.assertEqual(len(fixture.writes), 1, "VALID_CURRENT_PRODUCER_BOUNDARY_NOT_REACHED")
        self.assertEqual(fixture.caller, {"sentinel": "unrelated uncommitted caller work"})
        self.assertEqual(fixture.commits, [])
        self.assertEqual(fixture.global_rollbacks, [],
            "GLOBAL_ROLLBACK_ATTEMPT_BLOCKED_NOT_REAL_SQL_ERASURE; actual caught=" + type(caught).__name__)
        self.assertIs(caught, original, "Original exception object/type must reach the OUTER owner")

    def test_known_own_parent_is_physically_guarded_before_first_existing_write(self):
        original = WriteBoundaryReached("first current business write reached")
        fixture = Fixture(original)
        with self.assertRaises(WriteBoundaryReached) as caught:
            fixture.invoke("json")
        self.assertIs(caught.exception, original)
        self.assertEqual(len(fixture.writes), 1)
        self.assertIn(("root", "read"), fixture.access)
        self.assertIn(("root", "write"), fixture.access)
        updates = [(query, params) for query, params in fixture.cr.calls
                   if query.lstrip().upper().startswith("UPDATE MERCADOLIBRE_ORDERS")
                   and "write_date" in query.lower() and params and 12 in params]
        self.assertEqual(len(updates), 1, "MISSING_PHYSICAL_KNOWN_PARENT_GUARD_BEFORE_CURRENT_WRITE")

    def test_current_json_preserves_original_runtime_and_caller_control(self):
        self.assert_original_transport("json", RuntimeError)

    def test_installed_bridge_wrapper_preserves_original_serialization_control(self):
        self.assert_original_transport("wrapper", SerializationFailure)

    def test_polling_preserves_scalar_serialization_without_global_rollback_or_nested_retry(self):
        self.assert_original_transport("polling", SerializationFailure)

    def test_recent_preserves_scalar_runtime_without_global_rollback(self):
        self.assert_original_transport("recent", RuntimeError)

    def test_recent_preserves_scalar_deadlock_without_global_rollback(self):
        self.assert_original_transport("recent", DeadlockDetected)

    def test_actual_wizard_preserves_scalar_serialization_without_global_rollback(self):
        self.assert_original_transport("wizard", SerializationFailure)

    def test_actual_wizard_preserves_unique_violation_not_completed_outcome(self):
        self.assert_original_transport("wizard", UniqueViolation)

    def test_single_resync_catch_preserves_scalar_runtime_transport_only(self):
        # This does not accept its separate same-status skip/direct-cancel/prewrite behavior.
        self.assert_original_transport("resync", RuntimeError)

    def test_existing_normal_empty_scalar_return_is_not_failure_classified(self):
        fixture = Fixture(RuntimeError("must not be reached"))
        fixture.model.orders_update_order_json = lambda *args, **kwargs: {}
        self.assertEqual(fixture.invoke("wrapper"), [])
        self.assertEqual((fixture.writes, fixture.global_rollbacks, fixture.commits), ([], [], []))

    def test_known_remote_id_false_uses_one_fixed_parameterized_parent_update(self):
        failure = WriteBoundaryReached("existing parent replay")
        fixture = Fixture(failure)
        with self.assertRaises(WriteBoundaryReached) as caught:
            fixture.model.orders_update_order_json({"id": False, "order_json": fixture.body},
                config=fixture.company, meli=fixture.client)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.updates(fixture), [(
            "UPDATE mercadolibre_orders SET write_date=write_date WHERE id=%s RETURNING id", [12])])
        for name in ("order", "root", "line", "source"):
            self.assertIn((name, "read"), fixture.access)
            self.assertIn((name, "write"), fixture.access)

    def test_native_actor_company_and_circuit_boundaries_precede_update(self):
        for field in ("sudo", "company", "root_b", "context_b"):
            with self.subTest(boundary=field):
                fixture = Fixture(RuntimeError("must not reach business write"))
                if field == "sudo":
                    fixture.env.su = True
                elif field == "company":
                    fixture.model.company_id = SimpleNamespace(id=2)
                elif field == "root_b":
                    fixture.root[0].viarengo_circuit_code = "b"
                else:
                    fixture.env.context["default_viarengo_circuit_code"] = "b"
                self.assert_denied_before_physical_write(fixture)

    def test_foreign_payload_and_contradictory_local_remote_identities_refuse(self):
        for field in ("foreign", "local", "remote"):
            with self.subTest(identity=field):
                fixture = Fixture(RuntimeError("must not reach business write"))
                data = {"id": 12, "remote_order_id": "101", "order_json": fixture.body}
                if field == "foreign":
                    fixture.body["seller"]["id"] = 99
                elif field == "local":
                    data["id"] = 99
                else:
                    data["remote_order_id"] = "102"
                self.assert_denied_before_physical_write(fixture, data)

    def test_ambiguous_visible_parent_refuses_before_physical_write(self):
        fixture = Fixture(RuntimeError("must not reach business write"))
        other = SimpleNamespace(id=13, check_access=lambda mode: None)
        fixture.model.search = lambda *args, **kwargs: Rows([fixture.model, other])
        self.assert_denied_before_physical_write(fixture)

    def test_rule_hidden_parent_is_integrity_denial_not_duplicate_create(self):
        fixture = Fixture(RuntimeError("must not reach business write"))
        original = fixture.cr.fetchone
        fixture.cr.fetchone = lambda: (True,) if "FROM mercadolibre_orders WHERE order_id=%s" in fixture.cr.calls[-1][0] else original()
        self.assert_denied_before_physical_write(fixture)

    def test_hidden_line_or_source_population_refuses_before_update(self):
        fixture = Fixture(RuntimeError("must not reach business write"))
        original = fixture.cr.fetchone
        fixture.cr.fetchone = lambda: (True,) if "UNION ALL SELECT 1 FROM mercadolibre_order_items" in fixture.cr.calls[-1][0] else original()
        self.assert_denied_before_physical_write(fixture)

    def test_anonymous_or_contradictory_bound_source_is_not_adopted(self):
        for field in ("anonymous", "publication", "variation", "product"):
            with self.subTest(source=field):
                fixture = Fixture(RuntimeError("must not reach business write"))
                line = fixture.root.order_line[0]
                if field == "anonymous":
                    line.meli_source_order_item_id = False
                elif field == "publication":
                    fixture.body["order_items"][0]["item"]["id"] = "MLA999"
                elif field == "variation":
                    fixture.body["order_items"][0]["item"]["variation_id"] = 9
                else:
                    line.product_id = SimpleNamespace(id=999)
                self.assert_denied_before_physical_write(fixture)

    def test_first_commercial_projection_is_explicitly_not_guarded(self):
        failure = WriteBoundaryReached("existing unbound parent projection")
        fixture = Fixture(failure)
        fixture.model.sale_order = Rows()
        fixture.env["sale.order"].search = lambda *args, **kwargs: Rows()
        with self.assertRaises(WriteBoundaryReached) as caught:
            fixture.invoke("json")
        self.assertIs(caught.exception, failure)
        self.assertFalse(getattr(failure, "_meli_individual_operation_failure", False))
        self.assertEqual(self.updates(fixture), [])

    def test_pack_routes_keep_existing_producer_without_individual_guard(self):
        fixture = Fixture(RuntimeError("must not reach scalar buyer write"))
        fixture.body["pack_id"] = 900
        fixture.model._meli_import_pack = lambda *args: {"commercial_group": "existing route control"}
        self.assertEqual(fixture.invoke("json"), {"commercial_group": "existing route control"})
        self.assertEqual(self.updates(fixture), [])

    def test_physical_guard_error_preserves_original_code_and_caller_before_business(self):
        fixture = Fixture(RuntimeError("must not reach buyer write"))
        original = SerializationFailure("physical row version collision")
        execute = fixture.cr.execute
        def fail_update(query, params=None):
            execute(query, params)
            if query.startswith("UPDATE mercadolibre_orders"):
                raise original
        fixture.cr.execute = fail_update
        with self.assertRaises(SerializationFailure) as caught:
            fixture.invoke("json")
        self.assertIs(caught.exception, original)
        self.assertEqual(caught.exception.pgcode, "40001")
        self.assertTrue(caught.exception._meli_individual_operation_failure)
        self.assertEqual((fixture.writes, fixture.global_rollbacks, fixture.commits), ([], [], []))
        self.assertEqual(fixture.caller, {"sentinel": "unrelated uncommitted caller work"})

    def test_missing_update_row_is_refusal_not_false_completed_result(self):
        fixture = Fixture(RuntimeError("must not reach buyer write"))
        fixture.cr.rowcount = 0
        with self.assertRaises(Denied) as caught:
            fixture.invoke("json")
        self.assertTrue(caught.exception._meli_individual_operation_failure)
        self.assertEqual((fixture.writes, fixture.global_rollbacks, fixture.commits), ([], [], []))

    def test_actual_registered_bridge_layer_is_the_public_json_override(self):
        fixture = Fixture(RuntimeError("not executed"))
        self.assertEqual(type(fixture.model).__name__, "IndividualSerialization")
        self.assertEqual(Path(type(fixture.model).orders_update_order_json.__code__.co_filename),
                         BRIDGE / "addons/viarengo_circuit_meli/models/individual_serialization.py")


if __name__ == "__main__":
    unittest.main()
