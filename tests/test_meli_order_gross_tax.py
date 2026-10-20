"""Execute import pricing with tax-engine doubles, not native Odoo proof."""
import ast
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]


def operation(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf8"))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == name)
    method.decorator_list = []
    exec(compile(ast.fix_missing_locations(ast.Module([method], [])), path, "exec"), namespace)
    return namespace[name]


class Taxes(list):
    """A bounded response double: inversion belongs to native Odoo, not this test."""
    def __init__(self, company, included=False, base=1000 / 1.21, included_amount=0):
        super().__init__([SimpleNamespace(company_id=company, type_tax_use="sale",
            price_include=included, amount_type="percent", amount=21)])
        self.calls, self.base, self.included_amount = [], base, included_amount
        self.forward = 1000
        self.rounded_forward = None
        self.rounded_inverse = None
        self.rounded_calls = []

    def filtered(self, predicate):
        self[:] = [tax for tax in self if predicate(tax)]
        return self

    def _get_tax_details(self, price_unit, quantity, **kwargs):
        self.calls.append((price_unit, quantity, kwargs))
        return {"total_excluded": self.base * quantity,
                "total_included": self.forward * quantity,
                "taxes_data": [{"tax": self[0], "price_include": True,
                                "tax_amount": self.included_amount * quantity}]}

    def _prepare_base_line_for_taxes_computation(self, record, **values):
        return values

    def _add_tax_details_in_base_line(self, line, company, rounding_method=None):
        details = self._get_tax_details(line["price_unit"], line["quantity"],
            special_mode=line.get("special_mode", False), rounding_method=rounding_method)
        line["tax_details"] = {
            "total_excluded_currency": details["total_excluded"],
            "total_included_currency": details["total_included"],
            "delta_total_excluded_currency": 0,
            "taxes_data": [{"tax": row["tax"], "tax_amount_currency": row["tax_amount"]}
                           for row in details["taxes_data"]]}

    def _round_base_lines_tax_details(self, lines, company):
        for line in lines:
            self.rounded_calls.append(line.get("special_mode", False))
            if line.get("special_mode") and self.rounded_inverse is not None:
                line["tax_details"] = self.rounded_inverse
            elif not line.get("special_mode") and self.rounded_forward is not None:
                line["tax_details"]["total_included_currency"] = self.rounded_forward(line["price_unit"], line["quantity"])


class GrossOrderPricingTests(unittest.TestCase):
    def fixture(self, flag="tax_included"):
        currency = SimpleNamespace(rounding=0.01,
            compare_amounts=lambda first, second: round(first - second, 2))
        company = SimpleNamespace(id=1, _fields={}, currency_id=currency,
                                  tax_calculation_rounding_method="round_per_line",
                                  mercadolibre_tax_included=flag)
        taxes = Taxes(company)
        product = SimpleNamespace(taxes_id=taxes, uom_id=SimpleNamespace(id=1))
        product.product_tmpl_id = product
        price_field = SimpleNamespace(convert_to_cache=lambda value, record: value)
        class Environment(dict):
            pass
        env = Environment({"sale.order.line": SimpleNamespace(_fields={"price_unit": price_field})})
        env.company = company
        env.user = SimpleNamespace(company_id=company, has_group=lambda name: False)
        model = SimpleNamespace(env=env)
        root = SimpleNamespace(company_id=company, currency_id=currency,
                               partner_id=SimpleNamespace(id=7), fiscal_position_id=False)
        scope = {"Decimal": Decimal, "InvalidOperation": InvalidOperation,
                 "isfinite": isfinite, "ValidationError": ValueError,
                 "ml_tax_excluded": lambda model, config: flag == "tax_excluded",
                 "map_tax_compat": lambda position, current, *args: position.map_tax(current)}
        return model, company, product, root, scope

    def test_gross_import_does_not_depend_on_publication_flag(self):
        for flag in ("tax_included", "tax_excluded", "auto"):
            with self.subTest(flag=flag):
                model, company, product, root, scope = self.fixture(flag)
                convert = operation("models/versions.py", "ml_product_price_conversion", scope)
                price = convert(model, product, 1000, config=company)
                self.assertAlmostEqual(price, 1000 / 1.21, places=10)
                self.assertEqual(product.taxes_id.calls[0][2]["special_mode"], "total_included")
                self.assertEqual(product.taxes_id.calls[0][2]["rounding_method"], "round_globally")
                self.assertEqual(product.taxes_id.rounded_calls, [False])

    def test_effective_mapping_and_original_included_flags_are_used(self):
        model, company, product, root, scope = self.fixture()
        mapped = Taxes(company, included=True, base=1000 / 1.21, included_amount=1000 - 1000 / 1.21)
        root.fiscal_position_id = SimpleNamespace(map_tax=lambda taxes: mapped)
        convert = operation("models/versions.py", "ml_product_price_conversion", scope)
        self.assertAlmostEqual(convert(model, product, 1000, config=company,
                                       sale_order=root, quantity=3), 1000, places=10)
        self.assertEqual(product.taxes_id.calls, [])
        self.assertEqual(mapped.calls[0][1], 3)
        # Native forced inverse row price_include is true even for excluded taxes.
        mapped[0].price_include = False
        self.assertAlmostEqual(convert(model, product, 1000, config=company,
                                       sale_order=root, quantity=3), 1000 / 1.21, places=10)

    def test_actual_price_field_normalization_and_forward_refusal(self):
        model, company, product, root, scope = self.fixture()
        model.env["sale.order.line"]._fields["price_unit"].convert_to_cache = lambda price, record: round(price, 2)
        convert = operation("models/versions.py", "ml_product_price_conversion", scope)
        self.assertEqual(convert(model, product, 1000, config=company, sale_order=root), 826.45)
        product.taxes_id.forward = 1000.01
        with self.assertRaisesRegex(ValueError, "reconstruct"):
            convert(model, product, 1000, config=company, sale_order=root, quantity=3)

    def test_compound_native_line_rounding_uses_one_inverse_delta_or_refuses(self):
        """Native API response regression, not execution of Odoo or tax math."""
        model, company, product, root, scope = self.fixture()
        company.tax_calculation_rounding_method = "round_globally"
        taxes = product.taxes_id
        taxes.base = (3000 / (1.21 * 1.105)) / 3
        # Official native line rounding: 2243.75 + 471.19 + 285.07 = 3000.01.
        # Included-mode native base delta is -0.01; no local tax inverse here.
        taxes.rounded_inverse = {
            "total_excluded_currency": 2243.75, "delta_total_excluded_currency": -0.01,
            "taxes_data": [{"tax": taxes[0], "tax_amount_currency": 756.26}]}
        corrected = 2243.74 / 3
        taxes.rounded_forward = lambda unit, quantity: 3000 if unit == corrected else 3000.01
        convert = operation("models/versions.py", "ml_product_price_conversion", scope)
        self.assertEqual(convert(model, product, "1000", sale_order=root, quantity=3), corrected)
        self.assertEqual(taxes.rounded_calls, [False, "total_included", False])
        taxes.rounded_forward = lambda unit, quantity: 3000.01
        with self.assertRaisesRegex(ValueError, "reconstruct"):
            convert(model, product, "1000", sale_order=root, quantity=3)

    def test_invalid_prices_and_foreign_mapped_taxes_refuse(self):
        model, company, product, root, scope = self.fixture()
        convert = operation("models/versions.py", "ml_product_price_conversion", scope)
        for value in (True, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                convert(model, product, value, config=company, sale_order=root)
        mapped = Taxes(SimpleNamespace(id=2))
        root.fiscal_position_id = SimpleNamespace(map_tax=lambda taxes: mapped)
        with self.assertRaisesRegex(ValueError, "company"):
            convert(model, product, 1000, config=company, sale_order=root)

    def test_seller_aggregate_is_not_subtracted_from_discounted_unit_price(self):
        calls = []
        scope = {"ml_product_price_conversion": lambda model, **values: calls.append(values) or values["price"]}
        setter = operation("models/orders.py", "_set_product_unit_price", scope)
        model = SimpleNamespace(discount_seller_amount=20)
        result = setter(model, object(), {"unit_price": 100, "quantity": 2}, config=object())
        self.assertEqual(result["price_unit"], 100)
        self.assertEqual(calls[0]["quantity"], 2)

    def test_pack_source_keeps_explicit_full_price_without_inventing_missing_value(self):
        tree = ast.parse((ROOT / "models/orders.py").read_text(encoding="utf8"))
        pack = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                    and node.name == "_meli_import_pack")
        values = next(node for node in ast.walk(pack) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "source_values" for target in node.targets))
        present = next(node for node in ast.walk(pack) if isinstance(node, ast.If)
                       and ast.unparse(node.test) == "'full_unit_price' in item")
        for extra in ({}, {"full_unit_price": 1000}):
            item = {"item": {"id": "MLA123", "title": "Synthetic"}, "quantity": 1, "unit_price": 1000, **extra}
            scope = {"order": SimpleNamespace(id=1), "product": SimpleNamespace(id=2, name="Synthetic"),
                     "item": item, "variation": False, "source": {"currency_id": "ARS"}}
            exec(compile(ast.Module([values, present], []), "actual-pack-source", "exec"), scope)
            self.assertEqual(scope["source_values"]["unit_price"], "1000")
            self.assertEqual("full_unit_price" in scope["source_values"], bool(extra))
            if extra:
                self.assertEqual(scope["source_values"]["full_unit_price"], 1000)

    def test_import_and_pack_pass_sale_context_but_export_stays_separate(self):
        source = (ROOT / "models/orders.py").read_text(encoding="utf8")
        tree = ast.parse(source)
        importer = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                        and node.name == "orders_update_order_json")
        calls = [node for node in ast.walk(importer) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "_set_product_unit_price"]
        self.assertEqual(len(calls), 1)
        self.assertEqual({value.arg: ast.unparse(value.value) for value in calls[0].keywords}["sale_order"], "sorder")
        pack = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                    and node.name == "_meli_import_pack")
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "ml_product_price_conversion" for node in ast.walk(pack)))
        export = (ROOT / "models/product.py").read_text(encoding="utf8")
        self.assertIn("tax_excluded = ml_tax_excluded(self, config=config)", export)
        self.assertIn("new_price = txfixed + new_price * (1.0 + txpercent*0.01)", export)


if __name__ == "__main__":
    unittest.main()
