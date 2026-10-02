"""Execute the real order buyer branch without claiming an Odoo ORM runtime.

Run with python -B -m unittest discover -s tests -p test_order_buyer_binding.py.
The unchanged bridge first-ingress probe separately certifies persisted fields.
"""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest

ORDERS = Path(__file__).resolve().parents[1] / "models" / "orders.py"


def source_branch():
    tree = ast.parse(ORDERS.read_text(encoding="utf-8"), filename=str(ORDERS))
    methods = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef)
               and node.name == "orders_update_order_json"]
    if len(methods) != 1:
        raise AssertionError("one real order ingestion method required")
    method = methods[0]
    matches = [node for node in ast.walk(method) if isinstance(node, ast.If)
               and ast.dump(node.test) == ast.dump(
                   ast.parse("order and buyer_id", mode="eval").body)]
    if len(matches) != 1:
        raise AssertionError("one existing buyer update branch required")
    return method, matches[0]


def execute_branch(node, order, buyer, fields, initial_return):
    scope = {"order": order, "buyer_id": buyer, "order_fields": fields,
             "return_id": initial_return}
    code = compile(ast.fix_missing_locations(ast.Module(
        body=[deepcopy(node)], type_ignores=[])), str(ORDERS), "exec")
    exec(code, scope)
    return scope


class ExistingOrder:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def write(self, values):
        self.calls.append(values)
        if self.error is not None:
            raise self.error
        return self.result


class TestOrderBuyerBinding(unittest.TestCase):
    def test_fresh_singular_and_pack_payloads_gain_only_resolved_buyer(self):
        for fields in ({"order_id": "single", "order_items": [object()]},
                       {"order_id": "pack-child", "pack_id": "pack",
                        "order_items": [object()]}):
            with self.subTest(pack="pack_id" in fields):
                before = fields.copy()
                initial = object()
                scope = execute_branch(source_branch()[1], None,
                                       SimpleNamespace(id=71), fields, initial)
                self.assertIs(scope["order_fields"], fields)
                self.assertEqual(fields, dict(before, buyer=71))
                self.assertIs(scope["return_id"], initial)
                self.assertIsNone(scope["order"])

    def test_false_fresh_recordset_has_same_create_payload_behavior(self):
        fields = {}
        execute_branch(source_branch()[1], False, SimpleNamespace(id=71),
                       fields, None)
        self.assertEqual(fields, {"buyer": 71})

    def test_existing_order_keeps_write_once_return_and_payload_unchanged(self):
        result = object()
        order = ExistingOrder(result)
        fields = {"buyer": 13, "name": "unchanged"}
        before = fields.copy()
        scope = execute_branch(source_branch()[1], order, SimpleNamespace(id=71),
                               fields, object())
        self.assertEqual(order.calls, [{"buyer": 71}])
        self.assertIs(scope["return_id"], result)
        self.assertIs(scope["order"], order)
        self.assertEqual(fields, before)

    def test_repeated_existing_branch_never_creates_or_changes_payload(self):
        order = ExistingOrder(True)
        fields = {"name": "unchanged"}
        for _ in range(2):
            execute_branch(source_branch()[1], order, SimpleNamespace(id=71),
                           fields, None)
        self.assertEqual(order.calls, [{"buyer": 71}, {"buyer": 71}])
        self.assertEqual(fields, {"name": "unchanged"})
        # This boundary test does not claim persisted row/idempotence proof.

    def test_absent_buyer_preserves_fresh_and_existing_guards(self):
        for order in (None, False, ExistingOrder()):
            for buyer in (None, False):
                with self.subTest(existing=bool(order), buyer=buyer):
                    fields = {"name": "unchanged"}
                    initial = object()
                    scope = execute_branch(source_branch()[1], order, buyer,
                                           fields, initial)
                    self.assertEqual(fields, {"name": "unchanged"})
                    self.assertIs(scope["return_id"], initial)
                    if isinstance(order, ExistingOrder):
                        self.assertEqual(order.calls, [])

    def test_existing_write_error_propagates_identical_object_once(self):
        error = RuntimeError("bounded-test-error")
        order = ExistingOrder(error=error)
        fields = {}
        with self.assertRaises(RuntimeError) as caught:
            execute_branch(source_branch()[1], order, SimpleNamespace(id=71),
                           fields, None)
        self.assertIs(caught.exception, error)
        self.assertEqual(order.calls, [{"buyer": 71}])
        self.assertEqual(fields, {})

    def test_source_is_exact_existing_write_plus_fresh_assignment(self):
        _, branch = source_branch()
        expected = ast.parse(
            "if order and buyer_id:\n"
            "    return_id = order.write({'buyer': buyer_id.id})\n"
            "elif buyer_id:\n"
            "    order_fields['buyer'] = buyer_id.id\n").body[0]
        self.assertEqual(ast.dump(branch), ast.dump(expected))

    def test_branch_stays_in_buyer_block_before_same_payload_create(self):
        method, branch = source_branch()
        owners = [node for node in ast.walk(method) if isinstance(node, ast.If)
                  and any(child is branch for child in node.body)]
        self.assertEqual(len(owners), 1)
        self.assertTrue(any(isinstance(node, ast.Constant)
                            and node.value == "Buyer not fetched!"
                            for statement in owners[0].orelse
                            for node in ast.walk(statement)))
        creates = [node for node in ast.walk(method)
                   if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute)
                   and isinstance(node.func.value, ast.Name)
                   and node.func.value.id == "order_obj"
                   and node.func.attr == "create"]
        self.assertEqual(len(creates), 1)
        self.assertGreater(creates[0].lineno, branch.end_lineno)
        self.assertEqual(ast.dump(creates[0].args[0]),
                         ast.dump(ast.Name(id="order_fields", ctx=ast.Load())))

    def test_old_source_detector_exposes_missing_fresh_buyer(self):
        old = deepcopy(source_branch()[1])
        old.orelse = []
        fields = {}
        execute_branch(old, None, SimpleNamespace(id=71), fields, None)
        self.assertNotIn("buyer", fields)
        with self.assertRaises(AssertionError):
            self.assertEqual(fields.get("buyer"), 71)


if __name__ == "__main__":
    unittest.main()
