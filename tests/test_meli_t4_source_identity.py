"""Execute the real importer source-item block; not native ORM acceptance."""
import ast
import copy
from contextlib import contextmanager, nullcontext
from functools import wraps
from pathlib import Path
import re
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Denied(Exception):
    pass


def source_operation():
    """Baseline runs its existing block, never a missing proposed helper.

    After extraction into a private method, execute that exact method instead.
    Neither branch substitutes the actual selection/update implementation.
    """
    tree = ast.parse((ROOT / "models/orders.py").read_text(encoding="utf-8"))
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    namespace = {
        "ast": ast, "AccessError": Denied, "ValidationError": Denied,
        "AdmissionError": ValueError,
        "remote_id": lambda value: str(int(value)),
        "MeliCr": lambda record: record.env.cr,
    }
    helper = methods.get("_meli_upsert_source_item")
    if helper:
        helper.decorator_list = []
        exec(compile(ast.fix_missing_locations(ast.Module([helper], [])), "orders.py", "exec"), namespace)
        return namespace[helper.name]
    importer = methods["orders_update_order_json"]
    block = next(
        nodes for node in ast.walk(importer) for nodes in [getattr(node, "body", [])]
        if any(isinstance(value, ast.Assign) and isinstance(value.value, ast.BinOp)
               and isinstance(value.value.left, ast.Constant)
               and "FROM   mercadolibre_order_items" in str(value.value.left.value)
               for value in nodes)
    )
    start = next(i for i, node in enumerate(block) if isinstance(node, ast.Assign)
                 and any(isinstance(name, ast.Name) and name.id == "query" for name in node.targets))
    end = next(i for i in range(start, len(block)) if isinstance(block[i], ast.If)
               and ast.unparse(block[i].test) == "not order_item_ids")
    fragment = ast.unparse(ast.Module(block[start:end + 1], []))
    wrapper = "def operation(self, order, order_item_fields, config):\n"
    wrapper += "    order_items_obj = self.env['mercadolibre.order_items']\n"
    wrapper += "\n".join("    " + line for line in fragment.splitlines())
    wrapper += "\n    return order_item_id or order_item_ids\n"
    exec(compile(wrapper, "orders.py:existing-source-item-block", "exec"), namespace)
    return namespace["operation"]


class Records:
    def __init__(self, model, ids):
        self.model, self.ids = model, list(ids)

    def __len__(self):
        return len(self.ids)

    def __bool__(self):
        return bool(self.ids)

    def __iter__(self):
        return (Records(self.model, [value]) for value in self.ids)

    def __getattr__(self, name):
        if name == "id":
            return self.ids[0] if len(self.ids) == 1 else False
        return self.model.rows[self.ids[0]].get(name)

    def check_access(self, mode):
        if any(value in self.model.hidden for value in self.ids):
            raise Denied("hidden source")

    def filtered(self, predicate):
        return Records(self.model, [record.id for record in self if predicate(record)])

    def write(self, values):
        self.model.writes.append((self.ids[:], self.model.elevated))
        for value in self.ids:
            self.model.rows[value].update(values)
        return True


class Items:
    def __init__(self):
        self.rows, self.hidden, self.writes = {}, set(), []
        self.elevated = False

    def create(self, values):
        key = max(self.rows, default=0) + 1
        self.rows[key] = dict(values)
        return Records(self, [key])

    def browse(self, ids):
        return Records(self, ids if isinstance(ids, (tuple, list)) else [ids])

    def sudo(self):
        self.elevated = True
        return self

    def search(self, domain):
        return Records(self, [key for key, row in self.rows.items() if key not in self.hidden
                             and all(row.get(name) == value for name, operator, value in domain)])

    def flush_model(self, names):
        pass


class Cursor:
    def __init__(self, items):
        self.items, self.matches = items, []

    def execute(self, query, params=None):
        if params is None:
            publication = re.search(r"order_item_id = '([^']+)'", query)[1]
            order = int(re.search(r"AND order_id = (\d+)", query)[1])
            self.matches = [(key,) for key, row in self.items.rows.items()
                            if row["order_id"] == order and row["order_item_id"] == publication]
        else:
            order, publication, visible = params
            self.matches = [(any(key not in visible for key, row in self.items.rows.items()
                                 if row["order_id"] == order and row["order_item_id"] == publication),)]

    def fetchall(self):
        return self.matches

    def fetchone(self):
        return self.matches[0]

    def savepoint(self):
        return nullcontext()


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.items = Items()
        self.company, self.user = object(), object()
        self.order = SimpleNamespace(id=11, order_id="101", company_id=self.company,
                                     seller_id=self.user, check_access=lambda mode: None,
                                     ensure_one=lambda: None)
        self.config = SimpleNamespace(company_id=self.company, _fields={"company_id": True})
        class Environment(dict):
            pass
        self.env = Environment({"mercadolibre.order_items": self.items})
        self.env.company, self.env.user, self.env.cr = self.company, self.user, Cursor(self.items)
        self.model = SimpleNamespace(env=self.env, _meli_assert_order_admission_actor=lambda config: "42")
        self.model._meli_preflight_order_history = lambda *args, **kwargs: None
        self.operation = source_operation()

    def values(self, variation="7", order=11, quantity=2):
        return {"order_id": order, "order_item_id": "MLA123", "order_item_variation_id": variation,
                "quantity": quantity, "unit_price": "100"}

    def run_operation(self, values=None):
        return self.operation(self.model, self.order, self.values() if values is None else values, self.config)

    def test_stale_other_variation_refuses_without_overwrite_or_extra_source(self):
        old = self.items.create(self.values(variation="8", quantity=9))
        before = copy.deepcopy(self.items.rows[old.id])
        with self.assertRaises(Denied):
            self.run_operation()
        self.assertEqual(self.items.rows[old.id], before)
        self.assertEqual(len(self.items.rows), 1)

    def test_replay_updates_only_exact_source_order(self):
        own = self.items.create(self.values())
        sibling = self.items.create(self.values(order=12, quantity=9))
        self.run_operation(self.values(quantity=4))
        self.assertEqual(self.items.rows[own.id]["quantity"], 4)
        self.assertEqual(self.items.rows[sibling.id]["quantity"], 9)
        self.assertEqual(len(self.items.rows), 2)

    def test_duplicate_exact_identity_refuses_without_mutation(self):
        self.items.create(self.values())
        self.items.create(self.values())
        before = copy.deepcopy(self.items.rows)
        with self.assertRaises(Denied):
            self.run_operation(self.values(quantity=4))
        self.assertEqual(self.items.rows, before)

    def test_hidden_source_refuses_before_write(self):
        row = self.items.create(self.values())
        self.items.hidden.add(row.id)
        before = copy.deepcopy(self.items.rows)
        with self.assertRaises(Denied):
            self.run_operation()
        self.assertEqual(self.items.rows, before)

    def test_update_does_not_elevate_source_write(self):
        self.items.create(self.values())
        self.run_operation()
        self.assertTrue(self.items.writes)
        self.assertFalse(any(elevated for ids, elevated in self.items.writes))

    def test_null_and_empty_variation_share_absent_identity(self):
        source = self.items.create(self.values(variation=False))
        self.run_operation(self.values(variation="", quantity=4))
        self.assertEqual(len(self.items.rows), 1)
        self.assertEqual(self.items.rows[source.id]["quantity"], 4)

    def test_numeric_variation_is_normalized_without_duplicate(self):
        source = self.items.create(self.values())
        self.run_operation(self.values(variation="007", quantity=4))
        self.assertEqual(self.items.rows[source.id]["order_item_variation_id"], "7")
        self.assertEqual(len(self.items.rows), 1)


class ImportSavepointTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "models/orders.py").read_text(encoding="utf-8"))
        decorator = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                         and node.name == "_meli_order_import_savepoint")
        namespace = {"wraps": wraps}
        exec(compile(ast.Module([decorator], []), "real-import-savepoint", "exec"), namespace)
        self.decorate = namespace[decorator.name]
        self.state = {"caller": "retained"}
        @contextmanager
        def savepoint():
            before = copy.deepcopy(self.state)
            try:
                yield
            except BaseException:
                self.state.clear()
                self.state.update(before)
                raise
        self.model = SimpleNamespace(env=SimpleNamespace(cr=SimpleNamespace(savepoint=savepoint)))

    def test_exception_rolls_back_operation_not_caller_and_preserves_exception(self):
        error = Denied("original exception")
        @self.decorate
        def operation(model, data, context=None, config=None, meli=None):
            self.state["import"] = "partial"
            raise error
        with self.assertRaises(Denied) as caught:
            operation(self.model, {})
        self.assertIs(caught.exception, error)
        self.assertEqual(self.state, {"caller": "retained"})

    def test_normal_result_and_signature_metadata_are_preserved(self):
        result = {}
        @self.decorate
        def operation(model, data, context=None, config=None, meli=None):
            self.assertEqual((data, context, config, meli), (1, 2, 3, 4))
            self.state["import"] = "complete"
            return result
        self.assertIs(operation(self.model, 1, context=2, config=3, meli=4), result)
        self.assertEqual(operation.__name__, "operation")
        self.assertEqual(operation.__wrapped__.__name__, "operation")
        self.assertEqual(self.state, {"caller": "retained", "import": "complete"})

    def test_legacy_error_dictionary_is_not_reinterpreted_as_exception(self):
        result = {"error": "legacy normal return", "admission_pending": "pending"}
        @self.decorate
        def operation(model, data, context=None, config=None, meli=None):
            return result
        self.assertIs(operation(self.model, {}), result)
