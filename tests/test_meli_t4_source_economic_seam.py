"""Connector-only source economics must fail closed, with no bridge import."""
import ast
from pathlib import Path
import unittest


class ConnectorEconomicSeamTests(unittest.TestCase):
    def test_connector_only_seam_refuses_without_writes_or_dependencies(self):
        path = Path(__file__).resolve().parents[1] / "models/orders.py"
        tree = ast.parse(path.read_text(encoding="utf8"))
        line = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name == "sale_order_line")
        method = next(node for node in line.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_meli_source_economic_update")
        namespace = {"ValidationError": ValueError}
        exec(compile(ast.Module([method], []), str(path), "exec"), namespace)
        with self.assertRaisesRegex(ValueError, "adapter is required"):
            namespace[method.name](object(), 1, {"price_unit": 10})
        self.assertFalse(any(True for node in ast.walk(method) if isinstance(node, ast.Call)
                             if not (isinstance(node.func, ast.Name)
                                     and node.func.id == "ValidationError")))


if __name__ == "__main__":
    unittest.main()
