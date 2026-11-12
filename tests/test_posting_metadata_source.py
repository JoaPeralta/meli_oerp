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

        metadata = helper()
        method("posting.py", "_posting_update", vars(metadata))(Rows([row]))
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
            "seller_custom_field": "LEGACY", "variations": [],
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
        response.__enter__.return_value = response
        response.raw.read.return_value = json.dumps({"id": "MLA1"}).encode()
        adapter = MagicMock()
        adapter.send.return_value = response
        request = MagicMock()
        fake_requests = SimpleNamespace(
            Request=request, RequestException=OSError,
            adapters=SimpleNamespace(HTTPAdapter=lambda **kw: adapter),
        )
        with patch.dict(sys.modules, {"requests": fake_requests, "urllib3.exceptions":
                                       SimpleNamespace(HTTPError=OSError)}):
            self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), ({"id": "MLA1"}, None))
            self.assertEqual(request.call_args.args, ("GET", "https://api.mercadolibre.com/items/MLA1"))
            self.assertEqual(request.call_args.kwargs["headers"]["Accept-Encoding"], "identity")
            self.assertEqual(adapter.send.call_args.kwargs,
                             {"timeout": (5, 10), "stream": True, "verify": True, "proxies": {}})
            response.raw.read.assert_called_once_with(metadata.MAX_BODY + 1, decode_content=False)
            for status, reason in ((401, "authentication_rejected"), (302, "http_unavailable"),
                                   (503, "http_unavailable")):
                response.raw.read.reset_mock()
                response.status_code = status
                self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), (None, reason))
                response.raw.read.assert_not_called()
            response.status_code = 200
            response.headers = {"Content-Encoding": "gzip"}
            self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), (None, "invalid_response"))
            response.raw.read.assert_not_called()
            response.headers = {}
            response.raw.read.return_value = b"x" * (metadata.MAX_BODY + 1)
            self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), (None, "invalid_response"))
            response.raw.read.return_value = b"<html>error</html>"
            self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), (None, "invalid_response"))
            response.raw.read.return_value = b'{"id":"MLA1"}'
            for declared in ("5", "14"):
                response.headers = {"Content-Length": declared}
                self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), (None, "invalid_response"))
            response.headers = {"Content-Length": "13"}
            self.assertEqual(metadata.read_item_metadata("MLA1", "synthetic"), ({"id": "MLA1"}, None))
            self.assertTrue(adapter.close.called)

    def test_missing_variations_do_not_admit_root_offer_without_modern_identity(self):
        metadata = helper()
        payload = {"id": "MLA1", "seller_id": 42, "status": "active",
                   "price": 123, "seller_sku": "ROOT"}
        values, states, _, reason = metadata.metadata_evidence(payload, "MLA1", False, 42)
        self.assertIsNone(reason)
        self.assertEqual(values, {"meli_status": "active"})
        self.assertEqual(states["meli_price"], "unproven")
        for extra in ({"variations": []}, {"user_product_id": "MLAU789"}):
            values, _, _, reason = metadata.metadata_evidence({**payload, **extra}, "MLA1", False, 42)
            self.assertIsNone(reason)
            self.assertEqual(values["meli_price"], "123")
            self.assertEqual(values["meli_seller_custom_field"], "ROOT")
        _, _, _, reason = metadata.metadata_evidence(payload, "MLA1", "11", 42)
        self.assertEqual(reason, "unproven_variation")

    def test_user_product_owner_cannot_be_hidden_by_direct_match(self):
        other = object()
        error = ValueError("conflict")
        row = SimpleNamespace(
            meli_id="MLA1", meli_variation_id=False, product_id=None,
            ensure_one=lambda: None, _meli_binding_error=lambda: error,
            _meli_check_fallback_variant=lambda *args: product,
            _meli_find_catalog_product=lambda *args: other,
            _meli_check_binding_product=lambda *args: product,
        )
        company = object()
        product = SimpleNamespace(company_id=company, meli_user_product_id=False)
        row._meli_find_exact_posting = lambda *args: row
        row._meli_find_direct_product = lambda *args: product
        row._meli_find_reference_product = lambda *args: product
        identity = {"listing_with_variants": False, "sku_state": "valid", "sku_kind": "sku",
                    "sku": "SKU", "sku_keys": ["SKU"], "user_product_id": "MLAU777"}
        row._meli_catalogue_identity_matches = lambda *args: method(
            "posting.py", "_meli_catalogue_identity_matches", {},
        )(row, *args)
        with self.assertRaisesRegex(ValueError, "conflict"):
            method("posting.py", "_meli_metadata_binding", {"ITEM_ID": helper().ITEM_ID})(row, company, identity)

    def test_whitespace_is_incomplete_without_using_network(self):
        row = SimpleNamespace(meli_status=" \t", meli_permalink="https://example.test/", meli_price="1")
        metadata = helper()
        method("posting.py", "_posting_update", {"CORE_FIELDS": metadata.CORE_FIELDS,
                                                "is_missing": getattr(metadata, "is_missing", None)})([row])
        self.assertEqual(row.posting_update, "incomplete")

    def test_catalogue_and_direct_import_gate_item_before_using_remote_identity(self):
        for file, name, consumer in (
            ("company.py", "product_meli_get_products", "_meli_catalogue_matches"),
            ("product.py", "_meli_import_product_item", "_meli_find_exact_posting"),
        ):
            tree = ast.parse((ROOT / "models" / file).read_text(encoding="utf-8"))
            function = next(node for node in ast.walk(tree)
                            if isinstance(node, ast.FunctionDef) and node.name == name)
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
            gates = [node.lineno for node in calls if ast.unparse(node.func) == "admitted_catalogue_item"]
            uses = [node.lineno for node in calls if ast.unparse(node.func).endswith("." + consumer)]
            with self.subTest(file=file):
                self.assertTrue(gates and uses and min(gates) < min(uses))

    def test_legacy_custom_is_a_veto_not_positive_identity(self):
        product = SimpleNamespace(id=1, meli_user_product_id=False)
        other = SimpleNamespace(id=2)
        error = ValueError("conflict")
        row = SimpleNamespace(
            _meli_binding_error=lambda: error,
            _meli_find_reference_product=lambda sku, company: {"A": product, "B": other}.get(sku),
        )
        resolve = method("posting.py", "_meli_catalogue_identity_matches", {})
        self.assertEqual(resolve(row, object(), {"legacy_reference": "A"}, product), [])
        with self.assertRaisesRegex(ValueError, "conflict"):
            resolve(row, object(), {"sku_keys": ["A"], "legacy_reference": "B"}, product)
