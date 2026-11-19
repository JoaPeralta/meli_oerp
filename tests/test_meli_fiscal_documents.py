"""Fiscal-document wire contract; no credentials, Odoo, or network."""
import ast
import importlib
import io
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

try:
    import requests
except ImportError:
    requests = None


package = ModuleType("_meli_fiscal_source")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "models")]
sys.modules[package.__name__] = package
fiscal = importlib.import_module(package.__name__ + ".meli_fiscal_documents")

DOC = "17_41546004-a96d-4e38-938e-80a1c134fc5d"
OTHER = "d6ec81ef-9e92-4912-b6ed-4ca465d7e3d4"
FILENAME = "invoice-00000000000000000000000000000001.pdf"


class FiscalProtocolTest(unittest.TestCase):
    def test_pack_and_null_fallback_are_verified_against_own_order(self):
        order = {"id": 23, "seller": {"id": 17}, "pack_id": 71}
        self.assertEqual(fiscal.order_target(order, "23", "17", "71"), "71")
        self.assertEqual(fiscal.order_target(dict(order, pack_id=None), "23", "17", None), "23")
        for wrong in (dict(order, seller={"id": 18}), dict(order, id=24),
                      dict(order, pack_id=72), {"id": 23, "seller": {"id": 17}}):
            with self.subTest(order=wrong), self.assertRaises(fiscal.FiscalError):
                fiscal.order_target(wrong, "23", "17", "71")

    def test_missing_pack_never_means_null(self):
        with self.assertRaises(fiscal.FiscalError):
            fiscal.order_target({"id": 23, "seller": {"id": 17}}, "23", "17", None)

    def test_pack_must_cover_the_exact_complete_sale(self):
        body = {"id": 71, "status": "released", "orders": [{"id": 23}, {"id": 26}]}
        fiscal.verify_pack(body, "71", ("23", "26"))
        for members in (("23",), ("23", "24"), ("23", "23")):
            with self.subTest(members=members), self.assertRaises(fiscal.FiscalError):
                fiscal.verify_pack(body, "71", members)

    def test_only_explicit_empty_listing_or_bound_no_document_404_is_absent(self):
        message = "The pack_fiscal_document with pack_id: 71 does not exist"
        self.assertEqual(fiscal.pdf_documents(404, {"error": "not_found", "status": 404,
                                                   "message": message}, "71", "17"), {})
        self.assertEqual(fiscal.pdf_documents(200, {"pack_id": 71, "fiscal_documents": []},
                                              "71", "17"), {})
        for status in (403, 404, 500, 0):
            with self.subTest(status=status), self.assertRaises(fiscal.FiscalError):
                fiscal.pdf_documents(status, {}, "71", "17")

    def test_document_receipt_must_be_valid_and_read_back_from_same_resource(self):
        entry = {"id": DOC, "file_type": "application/pdf", "filename": FILENAME}
        listing = fiscal.pdf_documents(200, {"pack_id": 71, "fiscal_documents": [entry]}, "71", "17")
        self.assertEqual(fiscal.upload_receipt(201, {"ids": [DOC]}), DOC)
        self.assertEqual(fiscal.find_verified_document(listing, DOC, FILENAME), DOC)
        self.assertEqual(fiscal.find_verified_document(listing, None, FILENAME), DOC)
        self.assertIsNone(fiscal.find_verified_document(listing, DOC, "invoice-" + "1" * 32 + ".pdf"))
        self.assertIsNone(fiscal.find_verified_document(listing, OTHER, FILENAME))
        self.assertIsNone(fiscal.find_verified_document(listing, None, "unknown.pdf"))
        self.assertIsNone(fiscal.find_verified_document({}, DOC, FILENAME))
        for status, body in ((201, {}), (200, {"ids": ["x"]}), (409, {"ids": [DOC]}),
                             (403, {}), (500, {}), (0, {}), (201, {"ids": [DOC, OTHER]})):
            with self.subTest(status=status, body=body):
                self.assertIsNone(fiscal.upload_receipt(status, body))
        with self.assertRaises(fiscal.FiscalError):
            fiscal.pdf_documents(200, {"pack_id": 72, "fiscal_documents": [entry]}, "71", "17")

    def test_pdf_limits_and_envelope(self):
        good = b"%PDF-1.7\n" + b"x" * (1_000_000 - 16) + b"\n%%EOF\n"
        self.assertEqual(len(good), 1_000_000)
        self.assertIs(fiscal.check_pdf_envelope(good), good)
        for pdf in (b"", b"not a PDF", b"%PDF-1.7\ntruncated", good + b" "):
            with self.subTest(length=len(pdf)), self.assertRaises(fiscal.FiscalError):
                fiscal.check_pdf_envelope(pdf)

    def test_authorization_requires_the_matching_production_attempt(self):
        path = Path(package.__path__[0]) / "meli_fiscal_upload.py"
        tree = ast.parse(path.read_bytes())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == "_meli_fiscal_authorized")
        namespace = {"re": re}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
        check = namespace[method.name]

        class Attempts:
            rows = ()

            def sudo(self):
                return self

            def search_count(self, domain, limit):
                return min(limit, sum(all(row.get(key) == value for key, _op, value in domain)
                                      for row in self.rows))

        attempts = Attempts()
        move = SimpleNamespace(
            ensure_one=lambda: None, env={}, id=1, company_id=SimpleNamespace(id=2),
            l10n_ar_arca_state="authorized", l10n_ar_arca_cae="12345678901234",
            l10n_ar_arca_cae_due_date="2030-01-02",
        )
        move._fields = vars(move)
        self.assertFalse(check(move))
        move.env["l10n_ar.arca.attempt"] = attempts
        evidence = dict(move_id=1, company_id=2, state="authorized", environment="production",
                        cae=move.l10n_ar_arca_cae, cae_due_date=move.l10n_ar_arca_cae_due_date)
        for patch_values in ({"environment": "testing"}, {"company_id": 3}, {"move_id": 4},
                             {"state": "uncertain"}, {"cae": "55555555555555"}):
            attempts.rows = (dict(evidence, **patch_values),)
            self.assertFalse(check(move))
        attempts.rows = (evidence,)
        self.assertTrue(check(move))
        move.l10n_ar_arca_state = "pending"
        self.assertFalse(check(move))

    @unittest.skipUnless(requests, "requests is available in the Odoo test environment")
    def test_bearer_multipart_is_single_attempt_with_no_redirect_or_proxy(self):
        pdf = b"%PDF-1.7\nmock\n%%EOF"
        body = b'{"ids":["17_41546004-a96d-4e38-938e-80a1c134fc5d"]}'
        with patch("requests.adapters.HTTPAdapter") as adapter:
            reply = adapter.return_value.send.return_value.__enter__.return_value
            reply.status_code = 201
            reply.headers = {"Content-Length": str(len(body))}
            reply.raw = SimpleNamespace(read=lambda *_args, **_kw: body)
            status, result = fiscal.strict_fiscal_request(
                SimpleNamespace(access_token="synthetic-token"), "/packs/71/fiscal_documents",
                document=(FILENAME, pdf),
            )
            self.assertEqual(fiscal.upload_receipt(status, result), DOC)
            adapter.assert_called_once_with(max_retries=0)
            adapter.return_value.send.assert_called_once()
            call = adapter.return_value.send.call_args
            prepared = call.args[0]
            self.assertEqual(prepared.method, "POST")
            self.assertEqual(prepared.url, "https://api.mercadolibre.com/packs/71/fiscal_documents")
            self.assertEqual(prepared.headers["Authorization"], "Bearer synthetic-token")
            self.assertIn(b'name="fiscal_document"', prepared.body)
            self.assertIn(pdf, prepared.body)
            self.assertEqual(call.kwargs["proxies"], {})
            self.assertTrue(call.kwargs["verify"])

    @unittest.skipUnless(requests, "requests is available in the Odoo test environment")
    def test_redirect_or_incomplete_body_cannot_supply_receipt(self):
        with patch("requests.adapters.HTTPAdapter") as adapter:
            send = adapter.return_value.send
            reply = send.return_value.__enter__.return_value
            reply.status_code = 307
            reply.headers = {"Location": "https://example.invalid/collector"}
            reply.raw = None
            client = SimpleNamespace(access_token="synthetic-token")
            self.assertEqual(fiscal.strict_fiscal_request(client, "/packs/71/fiscal_documents"), (307, None))
            send.assert_called_once()
            reply.status_code = 200
            reply.headers = {"Content-Length": "300"}
            reply.raw = SimpleNamespace(read=lambda *_args, **_kw: io.BytesIO(b"{}").read())
            self.assertEqual(fiscal.strict_fiscal_request(client, "/packs/71/fiscal_documents"), (200, None))
            self.assertRaises(fiscal.FiscalError, fiscal.strict_fiscal_request, client,
                              "https://example.invalid/packs/71/fiscal_documents")
