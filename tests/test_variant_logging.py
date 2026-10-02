"""Exercise the real publication variant log call without an Odoo registry.

Run with python -B -m unittest discover -s tests -p test_variant_logging.py.
Only the actual AST call is executed; publication behavior is not simulated.
"""
import ast
from copy import deepcopy
import io
import logging
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PRODUCT = Path(__file__).resolve().parents[1] / "models" / "product.py"


def variant_call():
    tree = ast.parse(PRODUCT.read_text(encoding="utf-8"), filename=str(PRODUCT))
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name == "product_template_post"]
    if len(methods) != 1:
        raise AssertionError("one real publication method required")
    calls = [node for node in ast.walk(methods[0]) if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "_logger"
        and node.value.func.attr == "info" and len(node.value.args) == 3
        and isinstance(node.value.args[1], ast.Name) and node.value.args[1].id == "variant"
        and isinstance(node.value.args[2], ast.Attribute) and node.value.args[2].attr == "meli_pub"
        and isinstance(node.value.args[2].value, ast.Name) and node.value.args[2].value.id == "variant"]
    if len(calls) != 1:
        raise AssertionError("one exact variant publication log call required")
    return calls[0]


def execute_call(node, logger, variant):
    module = ast.Module(body=[node], type_ignores=[])
    code = compile(ast.fix_missing_locations(module), str(PRODUCT), "exec")
    exec(code, {"_logger": logger, "variant": variant})


class Variant:
    def __init__(self, published=True):
        self.meli_pub = published
        self.conversions = 0

    def __str__(self):
        self.conversions += 1
        return "bounded-variant"


class SurfacedErrorHandler(logging.StreamHandler):
    """Use the real formatter/emit path; logging failures must fail the test."""
    def __init__(self, stream):
        super().__init__(stream)
        self.records = []
        self.conversions_before_format = []
        self.errors = []

    def emit(self, record):
        self.records.append(record)
        self.conversions_before_format.append(record.args[0].conversions)
        return super().emit(record)

    def handleError(self, record):
        self.errors.append(sys.exc_info()[1])
        raise


class TestVariantLogging(unittest.TestCase):
    def setUp(self):
        # Private logger instance: do not mutate global logger configuration.
        self.stream = io.StringIO()
        self.logger = logging.Logger("variant-regression", logging.INFO)
        self.logger.propagate = False
        self.handler = SurfacedErrorHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger.addHandler(self.handler)
        self.addCleanup(self.handler.close)

    def test_actual_call_renders_once_with_original_lazy_arguments(self):
        variant = Variant()
        with patch.object(self.logger, "info", wraps=self.logger.info) as info, \
                patch.object(self.logger, "_log", wraps=self.logger._log) as logged:
            execute_call(variant_call(), self.logger, variant)
        self.assertEqual(info.call_count, 1)
        self.assertEqual(logged.call_count, 1)
        self.assertEqual(len(self.handler.records), 1)
        self.assertEqual(self.handler.conversions_before_format, [0])
        self.assertEqual(variant.conversions, 1)
        record = self.handler.records[0]
        self.assertIs(record.args[0], variant)
        self.assertIs(record.args[1], variant.meli_pub)
        self.assertEqual(record.msg, "Variant: %s %s")
        self.assertEqual(self.stream.getvalue(), "INFO Variant: bounded-variant True\n")
        self.assertEqual(self.handler.errors, [])

    def test_actual_call_preserves_false_publication_flag(self):
        variant = Variant(False)
        execute_call(variant_call(), self.logger, variant)
        self.assertEqual(self.stream.getvalue(), "INFO Variant: bounded-variant False\n")
        self.assertEqual(variant.conversions, 1)
        self.assertEqual(self.handler.errors, [])

    def test_disabled_info_does_not_convert_variant_or_dispatch(self):
        self.logger.setLevel(logging.WARNING)
        variant = Variant()
        with patch.object(self.logger, "info", wraps=self.logger.info) as info, \
                patch.object(self.logger, "_log", wraps=self.logger._log) as logged:
            execute_call(variant_call(), self.logger, variant)
        self.assertEqual(info.call_count, 1)
        self.assertEqual(logged.call_count, 0)
        self.assertEqual(variant.conversions, 0)
        self.assertEqual(self.handler.records, [])
        self.assertEqual(self.stream.getvalue(), "")

    def test_bad_fixture_detector_surfaces_the_same_type_error(self):
        node = deepcopy(variant_call())
        # Deliberately recreate only the old malformed message for detector self-test.
        node.value.args[0] = ast.copy_location(ast.Constant("Variant:"), node.value.args[0])
        with self.assertRaises(TypeError) as caught:
            execute_call(node, self.logger, Variant())
        self.assertEqual(len(self.handler.records), 1)
        self.assertEqual(len(self.handler.errors), 1)
        self.assertIs(self.handler.errors[0], caught.exception)
        self.assertEqual(self.stream.getvalue(), "")

    def test_call_remains_a_single_lazy_info_expression(self):
        node = variant_call()
        self.assertEqual(node.lineno, 242)
        self.assertIsInstance(node.value.args[0], ast.Constant)
        self.assertEqual(node.value.args[0].value, "Variant: %s %s")
        self.assertEqual(node.value.keywords, [])
        self.assertEqual(len(node.value.args), 3)


if __name__ == "__main__":
    unittest.main()
