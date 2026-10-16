"""Execute the real individual sale-line projection; no native ORM claims."""
import ast
import copy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "meli_source_order_item_id"


class Denied(Exception):
    pass


def source_line_operation():
    tree = ast.parse((ROOT / "models/orders.py").read_text(encoding="utf-8"))
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    namespace = {"AccessError": Denied, "ValidationError": Denied,
                 "SaleOrderLineTaxField": lambda model: "tax_ids",
                 "SaleOrderLineUomField": lambda model: "product_uom_id",
                 "tax_names_equivalent": lambda first, second: first == second,
                 "_logger": SimpleNamespace(info=lambda *args: None)}
    helper = methods.get("_meli_upsert_source_sale_line")
    if helper:
        helper.decorator_list = []
        exec(compile(ast.fix_missing_locations(ast.Module([helper], [])), "orders.py", "exec"), namespace)
        return namespace[helper.name]
    importer = methods["orders_update_order_json"]
    block = next(node for node in ast.walk(importer) if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "sorder and product_related_obj")
    # Baseline executes its existing importer block, not a missing helper.
    wrapper = "def operation(self, source, sorder, values, config):\n"
    wrapper += "    company = self.env.company\n"
    wrapper += "    saleorderline_obj = self.env['sale.order.line']\n"
    wrapper += "    product_related_obj = source.product_id\n"
    wrapper += "    order_item_iva = values.get('meli_order_item_iva', '')\n"
    wrapper += "    Item = {'item': {'id': source.order_item_id, 'variation_id': source.order_item_variation_id, 'title': 'synthetic'}, 'quantity': values['product_uom_qty']}\n"
    wrapper += "\n".join("    " + line for line in ast.unparse(block).splitlines())
    wrapper += "\n    return saleorderline_item_ids\n"
    exec(compile(wrapper, "orders.py:existing-sale-line-block", "exec"), namespace)
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

    def __or__(self, other):
        return Records(self.model, list(dict.fromkeys(self.ids + other.ids)))

    def __getitem__(self, key):
        return getattr(self, key)

    def __getattr__(self, key):
        if key == "id":
            return self.ids[0] if len(self.ids) == 1 else False
        value = self.model.rows[self.ids[0]].get(key)
        if key == SOURCE:
            return SimpleNamespace(id=value) if value else False
        if key == "order_id":
            return self.model.root if value == self.model.root.id else SimpleNamespace(id=value)
        if key == "product_id":
            return self.model.product if value == self.model.product.id else SimpleNamespace(id=value)
        if key == "company_id":
            return self.model.company if value == self.model.company.id else SimpleNamespace(id=value)
        return value

    def ensure_one(self):
        assert len(self.ids) == 1

    def check_access(self, mode):
        if set(self.ids) & self.model.hidden:
            raise Denied("hidden line")

    def write(self, values):
        self.model.generic_writes.append((self.ids[:], dict(values)))
        for key in self.ids:
            self.model.rows[key].update(values)
        return True

    def _meli_source_economic_update(self, source_id, values):
        self.ensure_one()
        if self.model.rows[self.id].get(SOURCE) != source_id:
            raise Denied("Exact source binding required")
        source = self.model.sources[source_id]
        if (source.quantity != values["product_uom_qty"]
                or str(values["price_unit"]) != source.unit_price):
            raise Denied("Incompatible source economics")
        self.model.economic_writes.append((self.id, source_id, dict(values)))
        if self.model.fail_economics:
            raise RuntimeError("late economic failure")
        self.model.rows[self.id].update(values)
        return True


class Lines:
    _fields = {SOURCE: True}

    def __init__(self, root, product, company, sources):
        self.root, self.product, self.company, self.sources = root, product, company, sources
        self.rows, self.hidden = {}, set()
        self.generic_writes, self.economic_writes = [], []
        self.fail_economics = False

    def create(self, values):
        key = max(self.rows, default=0) + 1
        self.rows[key] = dict(values, tax_ids=[], discount=0)
        return Records(self, [key])

    def search(self, domain, limit=None):
        ids = [key for key, row in self.rows.items() if key not in self.hidden
               and all(row.get(name) == value for name, operator, value in domain)]
        return Records(self, ids[:limit] if limit else ids)

    def flush_model(self, names):
        pass


class Cursor:
    def __init__(self, lines):
        self.lines = lines

    def execute(self, query, params):
        source, root, publication, visible = params
        self.found = any(key not in visible and (row.get(SOURCE) == source
                         or (row["order_id"] == root and row["meli_order_item_id"] == publication))
                         for key, row in self.lines.rows.items())

    def fetchone(self):
        return (self.found,)

    @contextmanager
    def savepoint(self):
        before = copy.deepcopy(self.lines.rows)
        try:
            yield
        except BaseException:
            self.lines.rows = before
            raise


class SourceImportTests(unittest.TestCase):
    def setUp(self):
        self.company = SimpleNamespace(id=1, _fields={})
        self.user = SimpleNamespace(id=5)
        self.root = SimpleNamespace(id=20, company_id=self.company, state="draft", locked=False,
                                    picking_ids=[], invoice_ids=[], meli_update_forbidden=False,
                                    meli_paid_amount=0, meli_discount_seller_amount=0,
                                    amount_total=0, _fields={},
                                    ensure_one=lambda: None, check_access=lambda mode: None)
        self.product = SimpleNamespace(id=2, uom_id=SimpleNamespace(id=1), taxes_id=[],
                                       display_name="Synthetic product")
        self.order = SimpleNamespace(id=10, company_id=self.company, seller_id=self.user,
                                     order_id="996100000001", sale_order=self.root,
                                     ensure_one=lambda: None, check_access=lambda mode: None)
        self.source = SimpleNamespace(id=30, order_id=self.order, product_id=self.product,
                                      order_item_id="MLA990000000001", order_item_variation_id="101",
                                      quantity=2, unit_price="100", ensure_one=lambda: None,
                                      check_access=lambda mode: None)
        self.lines = Lines(self.root, self.product, self.company, {30: self.source})
        class Environment(dict):
            pass
        self.env = Environment({"sale.order.line": self.lines})
        self.env.cr, self.env.company, self.env.user = Cursor(self.lines), self.company, self.user
        self.model = SimpleNamespace(env=self.env,
                                     _meli_assert_order_admission_actor=lambda config: "42",
                                     _meli_preflight_order_history=lambda *args, **kwargs: None,
                                     _set_product_unit_price=lambda **kwargs: {"price_unit": 100})
        self.operation = source_line_operation()

    def values(self, **extra):
        values = {"company_id": 1, "order_id": 20, "product_id": 2,
                  "meli_order_item_id": self.source.order_item_id,
                  "meli_order_item_variation_id": "101", "meli_order_item_iva": "",
                  "product_uom_id": 1, "product_uom_qty": 2, "price_unit": 100,
                  "name": "Synthetic product"}
        return dict(values, **extra)

    def run_operation(self, **extra):
        return self.operation(self.model, self.source, self.root, self.values(**extra), self.company)

    def test_first_import_binds_exact_source_then_replay_keeps_count(self):
        line = self.run_operation()
        self.assertEqual(self.lines.rows[line.id].get(SOURCE), self.source.id,
                         "FIRST_IMPORT_REMAINS_ANONYMOUS")
        replay = self.run_operation()
        self.assertEqual(replay.id, line.id)
        self.assertEqual(len(self.lines.rows), 1)

    def test_exact_source_update_preserves_same_publication_sibling(self):
        sibling = self.lines.create(self.values(**{SOURCE: 31}, product_uom_qty=9))
        own = self.lines.create(self.values(**{SOURCE: 30}))
        before = copy.deepcopy(self.lines.rows[sibling.id])
        self.source.quantity, self.source.unit_price = 4, "125"
        line = self.run_operation(product_uom_qty=4, price_unit=125)
        self.assertEqual(line.id, own.id, "PUBLICATION_SIBLING_SELECTED")
        self.assertEqual(self.lines.rows[sibling.id], before)
        self.assertEqual((self.lines.rows[own.id]["product_uom_qty"], self.lines.rows[own.id]["price_unit"]), (4, 125))
        self.assertEqual(self.lines.generic_writes, [])

    def test_anonymous_history_is_never_adopted(self):
        self.lines.create(self.values())
        before = copy.deepcopy(self.lines.rows)
        with self.assertRaises(Denied):
            self.run_operation()
        self.assertEqual(self.lines.rows, before)

    def test_duplicate_exact_source_is_not_a_limit_one_winner(self):
        for number in range(2):
            self.lines.create(self.values(**{SOURCE: 30}))
        before = copy.deepcopy(self.lines.rows)
        with self.assertRaises(Denied):
            self.run_operation()
        self.assertEqual(self.lines.rows, before)

    def test_hidden_exact_source_is_not_duplicated(self):
        line = self.lines.create(self.values(**{SOURCE: 30}))
        self.lines.hidden.add(line.id)
        before = copy.deepcopy(self.lines.rows)
        with self.assertRaises(Denied):
            self.run_operation()
        self.assertEqual(self.lines.rows, before)

    def test_wrong_root_exact_source_is_not_reparented(self):
        self.lines.create(self.values(**{SOURCE: 30}, order_id=21))
        before = copy.deepcopy(self.lines.rows)
        with self.assertRaises(Denied):
            self.run_operation()
        self.assertEqual(self.lines.rows, before)

    def test_contradictory_exact_source_product_refuses(self):
        self.lines.create(self.values(**{SOURCE: 30}, product_id=99))
        before = copy.deepcopy(self.lines.rows)
        with self.assertRaises(Denied):
            self.run_operation()
        self.assertEqual(self.lines.rows, before)

    def test_late_economic_failure_rolls_back_new_line(self):
        self.lines.fail_economics = True
        with self.assertRaisesRegex(RuntimeError, "late economic failure"):
            self.run_operation()
        self.assertEqual(self.lines.rows, {})

    def test_unsupported_price_transform_does_not_leave_new_line(self):
        with self.assertRaises(Denied):
            self.run_operation(price_unit=99)
        self.assertEqual(self.lines.rows, {})


class SourceImportWiringTests(unittest.TestCase):
    def test_full_importer_passes_its_actual_source_to_private_projection(self):
        tree = ast.parse((ROOT / "models/orders.py").read_text(encoding="utf-8"))
        importer = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                        and node.name == "orders_update_order_json")
        calls = [node for node in ast.walk(importer) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "_meli_upsert_source_sale_line"]
        self.assertEqual(len(calls), 1)
        self.assertEqual([ast.unparse(value) for value in calls[0].args],
                         ["order_item_id", "sorder", "saleorderline_item_fields", "config"])
        source_assignment = next(node for node in ast.walk(importer) if isinstance(node, ast.Assign)
                                 and isinstance(node.value, ast.Call)
                                 and isinstance(node.value.func, ast.Attribute)
                                 and node.value.func.attr == "_meli_upsert_source_item")
        self.assertEqual(ast.unparse(source_assignment.targets[0]), "order_item_id")
        self.assertLess(source_assignment.lineno, calls[0].lineno)


class IndividualRootGuardTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "models/orders.py").read_text(encoding="utf-8"))
        helper = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                      and node.name == "_meli_assert_individual_sale_root")
        helper.decorator_list = []
        class Query(str):
            def format(self, **values):
                return Query(super().format(**values))
        namespace = {"ValidationError": Denied,
                     "sql": SimpleNamespace(SQL=Query, Identifier=str)}
        exec(compile(ast.fix_missing_locations(ast.Module([helper], [])), "orders.py", "exec"), namespace)
        self.operation = namespace[helper.name]
        membership = SimpleNamespace(relation="sale_source_rel", column1="sale_id", column2="source_id")
        self.root = SimpleNamespace(id=20, _fields={"meli_orders": membership},
                                    ensure_one=lambda: None, check_access=lambda mode: None,
                                    flush_recordset=lambda names: None)
        self.order = SimpleNamespace(id=10, sale_order=self.root)
        self.references, self.members, self.bound_sources = [], [], []
        self.queries = []
        def execute(query, parameters):
            self.assertIn("mercadolibre_orders", query)
            self.assertIn("sale_source_rel", query)
            self.assertIn("line.meli_source_order_item_id", query)
            self.queries.append(parameters)
        def fetchone():
            return (any(value != self.order.id for values in (
                self.references, self.members, self.bound_sources) for value in values),)
        model = SimpleNamespace(flush_model=lambda names: None)
        lines = SimpleNamespace(flush_model=lambda names: None, _fields={SOURCE: True})
        class Environment(dict):
            pass
        self.env = Environment({"mercadolibre.orders": model,
                                "mercadolibre.order_items": model, "sale.order.line": lines})
        self.env.cr = SimpleNamespace(execute=execute, fetchone=fetchone)
        self.model = SimpleNamespace(env=self.env)

    def run_operation(self):
        return self.operation(self.model, self.order, self.root)

    def test_own_individual_references_remain_admitted(self):
        self.references.append(10)
        self.members.append(10)
        self.bound_sources.append(10)
        self.run_operation()
        self.assertEqual(self.queries, [[20, 10, 20, 10, 20, 10]])

    def test_another_order_reference_refuses_even_without_visible_membership(self):
        self.references.append(11)
        with self.assertRaises(Denied):
            self.run_operation()

    def test_another_many2many_member_refuses_without_order_backreference(self):
        self.members.append(11)
        with self.assertRaises(Denied):
            self.run_operation()

    def test_another_or_unknown_bound_source_refuses(self):
        for value in (11, None):
            with self.subTest(source=value):
                self.bound_sources[:] = [value]
                with self.assertRaises(Denied):
                    self.run_operation()

    def test_contradictory_root_refuses_before_query(self):
        self.order.sale_order = SimpleNamespace(id=21)
        with self.assertRaises(Denied):
            self.run_operation()
        self.assertEqual(self.queries, [])

    def test_missing_bridge_refuses(self):
        self.env["sale.order.line"]._fields = {}
        with self.assertRaises(Denied):
            self.run_operation()
