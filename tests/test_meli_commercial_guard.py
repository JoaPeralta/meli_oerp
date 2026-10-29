"""Actual lexical capability and pre-super methods; not native ACL/PG proof."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("economic_scope_actual", ROOT / "models/meli_economic_scope.py")
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class Rows(list):
    def __ior__(self, other):
        self.extend(row for row in other if row not in self)
        return self

    def filtered(self, predicate):
        return Rows(row for row in self if predicate(row))


class ProjectionGuardTests(unittest.TestCase):
    def setUp(self):
        self.env = SimpleNamespace(cr=object(), uid=7, company=SimpleNamespace(id=1), context={}, su=False)

        class Record:
            def __init__(record, env):
                record.env = env
            def with_context(record, **values):
                copied = SimpleNamespace(**vars(record.env))
                copied.context = dict(record.env.context, **values)
                return Record(copied)
        self.record = Record(self.env)

    def test_context_boolean_and_object_without_lexical_scope_are_not_grants(self):
        for fake in (True, object(), "trusted"):
            self.env.context["_meli_economic_projection"] = fake
            self.assertFalse(scope.economic_projection_allowed(self.env, "123"))

    def test_capability_expires_and_is_bound_to_exact_identity(self):
        with scope.economic_projection_lifetime():
            record = scope.activate_economic_projection(self.record, "123", 1)
            self.assertTrue(scope.economic_projection_allowed(record.env, "123"))
            self.assertFalse(scope.economic_projection_allowed(record.env, "124"))
            self.assertFalse(scope.economic_projection_allowed(self.env, "123"))
        self.assertFalse(scope.economic_projection_allowed(record.env, "123"))

    def test_cursor_actor_and_company_drift_never_reuse_authority(self):
        with scope.economic_projection_lifetime():
            record = scope.activate_economic_projection(self.record, "123", 1)
            for name, changed in (("cr", object()), ("uid", 8), ("company", SimpleNamespace(id=2)), ("su", True)):
                before = getattr(record.env, name)
                setattr(record.env, name, changed)
                self.assertFalse(scope.economic_projection_allowed(record.env, "123"))
                setattr(record.env, name, before)

    def guard_namespace(self):
        tree = ast.parse((ROOT / "models/meli_commercial_guard.py").read_bytes())
        nodes = [node for node in tree.body if isinstance(node, (ast.Assign, ast.FunctionDef, ast.ClassDef))]
        class Native:
            def __iter__(self):
                return iter((self,))
            def create(self, vals_list):
                return "native-create"
            def write(self, values):
                return "native-write"
        namespace = {"models": SimpleNamespace(Model=Native), "ValidationError": ValueError,
                     "api": SimpleNamespace(model_create_multi=lambda method: method),
                     "economic_projection_allowed": scope.economic_projection_allowed}
        exec(compile(ast.Module(nodes, []), "actual-commercial-guards", "exec"), namespace)
        return namespace

    def unrelated_environment(self):
        class UnrelatedSources(Rows):
            def browse(self, ids=None):
                if ids:
                    raise AssertionError("Unrelated native sale has no source IDs")
                return Rows()
            def search(self, domain):
                raise AssertionError("Unrelated native sale must not require MercadoLibre search access")
        return {"mercadolibre.orders": UnrelatedSources(), "sale.order.line": SimpleNamespace(_fields={})}

    def test_unrelated_native_root_with_false_remote_id_never_queries_ml_model(self):
        namespace = self.guard_namespace()
        root = SimpleNamespace(env=self.unrelated_environment(),
            mapped=lambda name: [False] if name == "meli_order_id" else Rows(), ids=[1])
        self.assertFalse(namespace["_sources"](root))

    def test_unrelated_direct_native_sale_create_retains_native_acl_path(self):
        namespace = self.guard_namespace()
        class Env(dict):
            context = {}
        model = namespace["EconomicSale"]()
        model.env = Env(self.unrelated_environment())
        self.assertEqual(model.create([{"state": "sale"}]), "native-create")

    def alias_fixture(self, namespace, *, current=False, defaults=None):
        source = SimpleNamespace(id=77, order_id="123", economic_snapshot={"status": "conditioned_draft"})
        class Sources(Rows):
            def browse(self, ids=None):
                return Sources([source]) if ids == 77 or ids == [77] else Sources()
            def search(self, domain):
                return Sources()
        class Env(dict):
            context = dict(defaults or {})
        env = Env({"mercadolibre.orders": Sources(), "sale.order.line": SimpleNamespace(_fields={})})
        env.cr, env.uid, env.company, env.su = self.env.cr, 7, self.env.company, False
        source.env = env
        root = namespace["EconomicSale"]()
        root.env, root.ids = env, [88]
        root.mapped = lambda name: [False] if name == "meli_order_id" else (
            Sources([source]) if name == "meli_order" and current else Sources())
        source.sale_order = root
        return root, source

    def test_singular_stored_alias_is_discovered_without_other_traceability(self):
        namespace = self.guard_namespace()
        root, source = self.alias_fixture(namespace, current=True)
        self.assertIn(source, namespace["_sources"](root))

    def test_singular_stored_alias_cannot_authorize_effects_as_unrelated_native(self):
        namespace = self.guard_namespace()
        root, _source = self.alias_fixture(namespace, current=True)
        with self.assertRaisesRegex(ValueError, "Economic"):
            namespace["_effects"](root)

    def test_explicit_and_default_singular_alias_creation_are_guarded(self):
        namespace = self.guard_namespace()
        for values, defaults in (({"meli_order": 77}, {}), ({}, {"default_meli_order": 77})):
            root, _source = self.alias_fixture(namespace, defaults=defaults)
            with self.subTest(defaults=defaults), self.assertRaisesRegex(ValueError, "Economic"):
                root.create([values])
        root, _source = self.alias_fixture(namespace, defaults={"default_meli_order": 77})
        self.assertEqual(root.create([{"meli_order": False}]), "native-create")

    def test_singular_alias_attachment_and_clear_are_guarded(self):
        namespace = self.guard_namespace()
        for current, value in ((False, 77), (True, False)):
            root, _source = self.alias_fixture(namespace, current=current)
            with self.subTest(current=current), self.assertRaisesRegex(ValueError, "Economic"):
                root.write({"meli_order": value})

    def test_item_and_payment_create_cannot_use_default_parent_to_bypass_audit_guard(self):
        namespace = self.guard_namespace()
        for model_name in ("EconomicItem", "EconomicPayment"):
            root, _source = self.alias_fixture(namespace, defaults={"default_order_id": 77})
            model = namespace[model_name]()
            model.env = root.env
            with self.subTest(model=model_name), self.assertRaisesRegex(ValueError, "Economic"):
                model.create([{}])
            self.assertEqual(model.create([{"order_id": False}]), "native-create")

    def test_actual_snapshot_clear_forgery_and_identity_edits_are_denied_before_super(self):
        namespace = self.guard_namespace()
        row = SimpleNamespace(env=self.env, order_id="123", economic_snapshot={"status": "conditioned_draft"})
        records = Rows([row])
        for values in ({"economic_snapshot": False}, {"economic_snapshot": {"status": "validated"}},
                       {"order_id": "124"}, {"company_id": 2}, {"sale_order": False}, {"total_amount": 1}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "Economic"):
                namespace["EconomicSource"].write(records, values)

    def test_actual_snapshot_create_forgery_and_default_forgery_are_denied(self):
        namespace = self.guard_namespace()
        model = SimpleNamespace(env=self.env)
        for values in ({"order_id": "123", "economic_snapshot": {"status": "validated"}}, {"order_id": "123"}):
            self.env.context["default_economic_snapshot"] = {"status": "validated"}
            with self.assertRaisesRegex(ValueError, "Economic"):
                namespace["EconomicSource"].create(model, [values])

    def test_actual_effect_guard_blocks_even_sql_fixture_without_version_marker(self):
        namespace = self.guard_namespace()
        root = SimpleNamespace()
        source = SimpleNamespace(sale_order=root, economic_snapshot={"status": "conditioned_draft"})
        namespace["_sources"] = lambda roots: Rows([source])
        with self.assertRaisesRegex(ValueError, "Economic"):
            namespace["_effects"]([root])

    def test_actual_latest_unknown_does_not_authorize_effect_from_historical_complete(self):
        namespace = self.guard_namespace()
        root = SimpleNamespace()
        source = SimpleNamespace(sale_order=root, economic_snapshot={"status": "validated",
            "latest_observation": {"status": "conditioned_draft"}})
        namespace["_sources"] = lambda roots: Rows([source])
        with self.assertRaisesRegex(ValueError, "Economic"):
            namespace["_effects"]([root])


if __name__ == "__main__":
    unittest.main()
