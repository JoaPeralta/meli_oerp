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
            def create(self, vals_list):
                return "native-create"
        namespace = {"models": SimpleNamespace(Model=Native), "ValidationError": ValueError,
                     "api": SimpleNamespace(model_create_multi=lambda method: method),
                     "economic_projection_allowed": scope.economic_projection_allowed}
        exec(compile(ast.Module(nodes, []), "actual-commercial-guards", "exec"), namespace)
        return namespace

    def unrelated_environment(self):
        class UnrelatedSources(Rows):
            def search(self, domain):
                raise AssertionError("Unrelated native sale must not require MercadoLibre search access")
        return {"mercadolibre.orders": UnrelatedSources(), "sale.order.line": SimpleNamespace(_fields={})}

    def test_unrelated_native_root_with_false_remote_id_never_queries_ml_model(self):
        namespace = self.guard_namespace()
        root = SimpleNamespace(env=self.unrelated_environment(),
            mapped=lambda name: Rows() if name == "meli_orders" else [False], ids=[1])
        self.assertFalse(namespace["_sources"](root))

    def test_unrelated_direct_native_sale_create_retains_native_acl_path(self):
        namespace = self.guard_namespace()
        class Env(dict):
            context = {}
        model = namespace["EconomicSale"]()
        model.env = Env(self.unrelated_environment())
        self.assertEqual(model.create([{"state": "sale"}]), "native-create")

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
