"""Execute only the native receiver branch, without Odoo or real API calls."""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]


def receiver_branch():
    path = ROOT / "models/orders.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "orders_update_order_json")
    initial = next(node for node in ast.walk(method) if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "Receiver" for target in node.targets)
                   and isinstance(node.value, ast.Constant) and node.value.value is False)
    branch = next(node for node in ast.walk(method)
                  if isinstance(node, ast.If) and node.lineno == initial.end_lineno + 1)
    return compile(ast.Module(body=[initial, branch], type_ignores=[]), str(path), "exec")


class RequestObserved(Exception):
    """Stop at the GET boundary; no API response is fabricated."""


class CachedShipment(SimpleNamespace):
    def __len__(self):
        return 1


class NullShipmentReceiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.branch = receiver_branch()
        tree = ast.parse((ROOT / "models/meli_shipment_format.py").read_text(encoding="utf-8"))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "SHIPMENTS_NEW_FORMAT_HEADERS"
                                  for target in node.targets))
        cls.headers = ast.literal_eval(assignment.value)

    def fixture(self, order, cached=None):
        searches, requests = [], []

        def search(domain, limit):
            searches.append((domain, limit))
            return cached if cached is not None else []

        def get(path, params, **options):
            requests.append((path, params, options))
            raise RequestObserved()

        namespace = {"order_json": order,
                     "self": SimpleNamespace(env={"mercadolibre.shipment": SimpleNamespace(search=search)}),
                     "meli": SimpleNamespace(access_token="TEST-ONLY", get=get),
                     "SHIPMENTS_NEW_FORMAT_HEADERS": self.headers}
        return namespace, searches, requests

    def test_null_empty_or_missing_id_keeps_receiver_fallback_without_lookup_or_get(self):
        cases = [{}, {"shipping": None}, {"shipping": {}}, {"shipping": {"id": None}},
                 {"shipping": {"id": ""}}, {"shipping": {"mode": "custom"}}]
        for order in cases:
            with self.subTest(order=order):
                before = deepcopy(order)
                namespace, searches, requests = self.fixture(order)
                exec(self.branch, namespace)
                self.assertIs(namespace["Receiver"], False)
                self.assertEqual(searches, [])
                self.assertEqual(requests, [])
                self.assertEqual(order, before)

    def test_inline_receiver_address_has_precedence_over_shipment_id(self):
        address = {"address_line": "TEST STREET"}
        for shipment_id in (None, "123"):
            with self.subTest(shipment_id=shipment_id):
                order = {"shipping": {"id": shipment_id, "receiver_address": address}}
                before = deepcopy(order)
                namespace, searches, requests = self.fixture(order)
                exec(self.branch, namespace)
                self.assertIs(namespace["Receiver"], address)
                self.assertEqual((searches, requests), ([], []))
                self.assertEqual(order, before)

    def test_valid_id_uses_cached_receiver_without_get(self):
        cached = CachedShipment(receiver_address_line="TEST STREET", receiver_address_name="TEST RECEIVER",
            receiver_address_phone="TEST PHONE", receiver_country_code="AR", receiver_country="Argentina",
            receiver_state="TEST STATE", receiver_state_code="TEST-S", receiver_city="TEST CITY",
            receiver_city_code="TEST-C")
        order = {"shipping": {"id": "123"}}
        before = deepcopy(order)
        namespace, searches, requests = self.fixture(order, cached)
        exec(self.branch, namespace)
        self.assertEqual(searches, [([("shipping_id", "=", "123")], 1)])
        self.assertEqual(namespace["Receiver"]["address_line"], cached.receiver_address_line)
        self.assertEqual(namespace["Receiver"]["country"], {"id": "AR", "name": "Argentina"})
        self.assertEqual(requests, [])
        self.assertEqual(order, before)

    def test_valid_uncached_id_reaches_native_get_with_new_format_header(self):
        order = {"shipping": {"id": "123"}}
        before = deepcopy(order)
        namespace, searches, requests = self.fixture(order)
        with self.assertRaises(RequestObserved):
            exec(self.branch, namespace)
        self.assertEqual(searches, [([("shipping_id", "=", "123")], 1)])
        self.assertEqual(requests, [("/shipments/123", {"access_token": "TEST-ONLY"},
                                    {"extra_headers": {"x-format-new": "true"}})])
        self.assertEqual(order, before)


if __name__ == "__main__":
    unittest.main()
