"""Execute the real delivery method with source-bound fakes, not Odoo ORM proof."""
import ast
from copy import deepcopy
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "models/shipment.py"


def real_method():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    methods = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "partner_delivery_id"]
    if len(methods) != 1:
        raise AssertionError("exactly one real delivery method required")
    scope = {"_logger": logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(ast.Module(
        body=[deepcopy(methods[0])], type_ignores=[])), str(SOURCE), "exec"), scope)
    return scope["partner_delivery_id"]


class Contact:
    def __init__(self, identity, values):
        self.id = identity
        self.values = dict(values)
        self._fields = set(values)
        self.writes = []

    def __len__(self):
        return 1

    def __getitem__(self, key):
        return self.values[key]

    def write(self, values):
        self.writes.append(dict(values))
        self.values.update(values)


class Partners:
    def __init__(self, existing=None):
        self.existing = existing
        self.created = []
        self.domains = []

    def search(self, domain, limit=None):
        self.domains.append(domain)
        return self.existing

    def create(self, values):
        row = Contact(73, values)
        self.created.append(row)
        self.existing = row
        return row


class Delivery:
    def __init__(self, existing=None):
        self.partners = Partners(existing)
        orders = SimpleNamespace(city=lambda _: "Fixture city", country=lambda _: 1,
            state=lambda country, receiver: 2, full_phone=lambda _: None,
            fix_locals=lambda _: {})
        # No actor company is supplied: ownership must come from the parent.
        self.env = {"res.partner": self.partners, "mercadolibre.orders": orders}

    def _get_or_create_delivery_address(self, parent_partner, shipping_vals):
        return None


RECEIVER = {"receiver_name": "Fixture delivery", "address_line": "Fixture street", "zip_code": "123"}


def parent(company=17):
    return SimpleNamespace(id=29, name="Fixture buyer", lang=False,
                           company_id=SimpleNamespace(id=company))


class TestShippingPartnerCompany(unittest.TestCase):
    def test_fresh_uses_parent_company_not_actor_or_default(self):
        for company in (17, 41, False):
            with self.subTest(company=company):
                model = Delivery()
                row = real_method()(model, parent(company), RECEIVER)
                self.assertIs(row, model.partners.created[0])
                self.assertEqual(row.values["company_id"], company)
                self.assertEqual(row.values["parent_id"], 29)
                self.assertEqual(row.values["type"], "delivery")
                self.assertEqual(len(model.partners.created), 1)

    def test_repeat_repairs_null_company_on_same_delivery_without_duplicate(self):
        model = Delivery()
        method = real_method()
        row = method(model, parent(), RECEIVER)
        row.values["company_id"] = False
        repeated = method(model, parent(), RECEIVER)
        self.assertIs(repeated, row)
        self.assertEqual(row.values["company_id"], 17)
        self.assertEqual(len(model.partners.created), 1)
        self.assertEqual(len(row.writes), 1)
        self.assertEqual(row.writes[0]["company_id"], 17)
        self.assertNotIn("parent_id", row.writes[0])
        self.assertNotIn("type", row.writes[0])

    def test_already_correct_repeat_preserves_row_and_fields(self):
        model = Delivery()
        method = real_method()
        row = method(model, parent(41), RECEIVER)
        before = row.values.copy()
        self.assertIs(method(model, parent(41), RECEIVER), row)
        self.assertEqual(row.values, before)
        self.assertEqual(row.writes, [])
        self.assertEqual(len(model.partners.created), 1)

    def test_parent_safety_guard_and_disabled_shipping_remain(self):
        model = Delivery(Contact(29, {"company_id": False}))
        self.assertIsNone(real_method()(model, parent(), RECEIVER))
        self.assertEqual(model.partners.existing.writes, [])
        self.assertIsNone(real_method()(model, parent(), RECEIVER,
            SimpleNamespace(mercadolibre_cron_get_orders_shipment_client=False)))
        self.assertEqual(model.partners.created, [])
        self.assertIsNone(real_method()(model, None, RECEIVER))
        self.assertIsNone(real_method()(model, parent(), None))

    def test_company_source_is_exact_parent_relation_without_sudo(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == "partner_delivery_id")
        payload = next(n.value for n in method.body if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "pdelivery_fields" for t in n.targets))
        company = [v for k, v in zip(payload.keys, payload.values)
                   if isinstance(k, ast.Constant) and k.value == "company_id"]
        self.assertEqual(len(company), 1)
        self.assertEqual(ast.dump(company[0]), ast.dump(ast.parse(
            "partner_id.company_id.id", mode="eval").body))
        self.assertNotIn("sudo", ast.unparse(method))


if __name__ == "__main__":
    unittest.main()
