"""Offline, source-bound regressions for posting read and import boundaries."""

import ast
import importlib.util
import json
from pathlib import Path
import symtable
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def method(file, name, namespace):
    tree = ast.parse((ROOT / "models" / file).read_text(encoding="utf-8"))
    node = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), file, "exec"), namespace)
    return namespace[name]


def helper():
    path = ROOT / "models" / "meli_posting_metadata.py"
    spec = importlib.util.spec_from_file_location("posting_metadata_offline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestPostingMetadataSource(unittest.TestCase):
    def test_posting_methods_do_not_shadow_translation_callable(self):
        path = ROOT / "models" / "posting.py"
        table = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
        for model in table.get_children():
            for function in model.get_children():
                with self.subTest(function=function.get_name()):
                    if "_" in function.get_identifiers():
                        self.assertFalse(function.lookup("_").is_local())

    def test_read_does_not_fetch_or_write(self):
        calls = []
        row = SimpleNamespace(
            meli_status=False, meli_price=False, meli_permalink=False,
            posting_query_questions=lambda: calls.append("network"),
        )

        class Rows(list):
            env = {"mercadolibre.posting": object()}
            env = type("Env", (dict,), {"user": SimpleNamespace(company_id=1)})(env)

        method("posting.py", "_posting_update", {})(Rows([row]))
        self.assertEqual(calls, [])
        self.assertNotEqual(row.posting_update, "ok")

    def test_existing_product_link_uses_item_metadata(self):
        tree = ast.parse((ROOT / "models" / "company.py").read_text(encoding="utf-8"))
        bulk = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "product_meli_get_products")
        creation = next(node for node in ast.walk(bulk)
                        if isinstance(node, ast.Call) and ast.unparse(node.func) == "postings.create")
        self.assertTrue(
            any(isinstance(node, ast.Name) and node.id == "metadata" for node in ast.walk(creation)),
            "the existing-product posting create payload omits normalized item metadata",
        )

    def test_variation_never_uses_sibling_sku_or_listing_price(self):
        metadata = helper()
        item = {
            "id": "MLA1", "seller_id": 42, "status": "active",
            "price": 100, "seller_sku": "LISTING",
            "variations": [{"id": 11, "seller_sku": "SIBLING"}, {"id": 12, "price": 20}],
        }
        values, states, _, error = metadata.metadata_evidence(item, "MLA1", "12", 42)
        self.assertIsNone(error)
        self.assertEqual(values["meli_price"], "20")
        self.assertEqual(states["meli_seller_custom_field"], "absent")
        _, states, _, _ = metadata.metadata_evidence(item, "MLA1", False, 42)
        self.assertEqual(states["meli_price"], "not_applicable")
        self.assertEqual(states["meli_seller_custom_field"], "not_applicable")

    def test_field_availability_and_no_unsafe_values(self):
        metadata = helper()
        item = {
            "id": "MLA1", "seller_id": 42, "status": "",
            "permalink": "https://articulo.mercadolibre.com.ar/MLA-1-x?access_token=secret",
            "seller_custom_field": "LEGACY",
            "attributes": [{"id": "SELLER_SKU", "values": [{"name": "EFFECTIVE"}]}],
        }
        values, states, identity, error = metadata.metadata_evidence(item, "MLA1", False, 42)
        self.assertIsNone(error)
        self.assertEqual(values, {"meli_seller_custom_field": "EFFECTIVE"})
        self.assertEqual(states, {"meli_status": "empty", "meli_price": "absent",
                                  "meli_permalink": "invalid", "meli_seller_custom_field": "valid"})
        self.assertEqual(identity["sku_kind"], "attribute")
        self.assertEqual(metadata.imported_metadata(item, 401, "MLA1", False, 42), {})
        self.assertEqual(metadata.imported_metadata(item, 200, "MLA1", False, 43), {})

    def test_explicit_transport_is_bounded_get_without_redirect_or_ambient_proxy(self):
        import sys
        from unittest.mock import MagicMock

        metadata = helper()
        response = MagicMock(status_code=200, headers={})
        response.iter_content.return_value = [json.dumps({"id": "MLA1"}).encode()]
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value.__enter__.return_value = response
        requests = SimpleNamespace(Session=lambda: session, RequestException=OSError)
        with patch.dict(sys.modules, requests=requests):
            self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), ({"id": "MLA1"}, None))
            self.assertFalse(session.trust_env)
            self.assertEqual(session.get.call_args.args, ("https://api.mercadolibre.com/items/MLA1",))
            self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
            self.assertTrue(session.get.call_args.kwargs["stream"])
            for status, reason in ((401, "authentication_rejected"), (302, "http_unavailable"),
                                   (503, "http_unavailable")):
                response.status_code = status
                self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), (None, reason))
            response.status_code = 200
            response.iter_content.return_value = [b"x" * (metadata.MAX_BODY + 1)]
            self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), (None, "invalid_response"))
            response.iter_content.return_value = [b"<html>error</html>"]
            self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), (None, "invalid_response"))
            session.post.assert_not_called()
            session.put.assert_not_called()
