"""Hermetic tests of actual P0 source methods (no Odoo DB or network).

Extract methods from the production AST. These checks supplement, not replace,
the existing Odoo 19 native suite and a later normal-user HOMO acceptance.
"""
import ast
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "FAKE-BEARER-P0-NOT-A-CREDENTIAL"


class AccessError(Exception):
    pass


class ValidationError(Exception):
    pass


class RequestException(Exception):
    pass


def actual(path, owner, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    cls = next(c for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == owner)
    matches = [f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == name]
    if len(matches) != 1:
        raise AssertionError("Missing exact production method " + name)
    node = deepcopy(matches[0])
    node.decorator_list = []
    exec(compile(ast.fix_missing_locations(
        ast.Module(body=[node], type_ignores=[])), str(ROOT / path), "exec"
    ), namespace)
    return namespace[name]


class Transport:
    def __init__(self, status=200, body=b"%PDF-1.4\nsynthetic", mime="application/pdf", error=None):
        self.response = NS(status_code=status, content=body, headers={"Content-Type": mime})
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class Group:
    def __init__(self, shipments):
        self.shipments = shipments

    def __len__(self):
        return len(self.shipments)

    def __iter__(self):
        return iter(self.shipments)


def label_method(transport):
    ns = {"AccessError": AccessError, "ValidationError": ValidationError,
          "requests": NS(get=transport.get, exceptions=NS(RequestException=RequestException))}
    return actual("models/shipment.py", "mercadolibre_shipment",
                  "_meli_fetch_label_bytes", ns)


class TestLabelTransport(unittest.TestCase):
    def setUp(self):
        self.company = NS(mercadolibre_seller_id="451")
        self.meli = NS(seller_id="451", access_token=TOKEN, need_login=lambda: False)

    def shipments(self, count=2):
        return Group([NS(shipping_id=str(100 + i),
                         _meli_check_label_scope=lambda config=None: self.company)
                      for i in range(count)])

    def test_batch_uses_fixed_host_bearer_and_no_token_query(self):
        transport = Transport()
        payload = label_method(transport)(self.shipments(), print_mode="pdf", meli=self.meli)
        self.assertEqual(payload, b"%PDF-1.4\nsynthetic")
        url, kw = transport.calls[0]
        self.assertEqual(url, "https://api.mercadolibre.com/shipment_labels")
        self.assertEqual(kw["params"], {"shipment_ids": "100,101", "response_type": "pdf"})
        self.assertEqual(kw["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertFalse(kw["allow_redirects"])
        self.assertGreater(kw["timeout"], 0)
        self.assertNotIn(TOKEN, url + json.dumps(kw["params"]))

    def test_zpl_and_zpl_txt_use_zpl2_parameter(self):
        for mode in ("zpl", "zpl_txt"):
            with self.subTest(mode=mode):
                transport = Transport(body=b"^XA^XZ", mime="text/plain")
                self.assertEqual(label_method(transport)(
                    self.shipments(1), print_mode=mode, meli=self.meli), b"^XA^XZ")
                self.assertEqual(transport.calls[0][1]["params"]["response_type"], "zpl2")

    def test_overlimit_wrong_seller_and_mixed_company_refuse_without_transport(self):
        for source, client in [
            (self.shipments(51), self.meli),
            (self.shipments(), NS(seller_id="452", access_token=TOKEN,
                                   need_login=lambda: False)),
            (Group([NS(shipping_id="123", _meli_check_label_scope=lambda config=None: self.company),
                    NS(shipping_id="124", _meli_check_label_scope=lambda config=None: NS(
                        mercadolibre_seller_id="999"))]), self.meli),
        ]:
            with self.subTest(case=len(source)):
                transport = Transport()
                with self.assertRaises((ValidationError, AccessError)):
                    label_method(transport)(source, meli=client)
                self.assertEqual(transport.calls, [])

    def test_provider_errors_are_generic_and_never_echo_token(self):
        for opts in [
            {"status": 403, "body": b"Authorization: " + TOKEN.encode()},
            {"mime": "text/html", "body": b"<h1>Oops</h1>"},
            {"error": RequestException("https://invalid/?access_token=" + TOKEN)},
        ]:
            with self.subTest(opts=sorted(opts)):
                transport = Transport(**opts)
                with self.assertRaises(ValidationError) as caught:
                    label_method(transport)(self.shipments(), meli=self.meli)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn("https://", str(caught.exception))


class Related(list):
    def __or__(self, other):
        return Related(list(self) + list(other))


def label_scope():
    return actual("models/shipment.py", "mercadolibre_shipment",
                  "_meli_check_label_scope",
                  {"AccessError": AccessError, "ValidationError": ValidationError})


class TestLabelRecordAuthorization(unittest.TestCase):
    def fixture(self, **overrides):
        seller = NS(id=18)
        company = NS(id=1, mercadolibre_seller_user=seller)
        order = NS(company_id=company, seller_id=seller, shipping_id="123",
                   check_access=Mock())
        shipment = NS(company_id=company, seller_id=seller, shipping_id="123",
                      order=Related([order]), orders=Related([]),
                      env=NS(company=company), check_access=Mock(), ensure_one=Mock())
        for key, value in overrides.items():
            setattr(shipment, key, value)
        return shipment, company, seller, order

    def test_valid_and_wrong_company_seller_shipping_relation(self):
        check = label_scope()
        shipment, company, seller, order = self.fixture()
        self.assertIs(check(shipment), company)
        shipment.check_access.assert_called_once_with("read")
        order.check_access.assert_called_once_with("read")
        for key, wrong in [("company_id", NS(id=2)),
                           ("seller_id", NS(id=19)),
                           ("shipping_id", "../456")]:
            with self.subTest(key=key):
                row, _, _, _ = self.fixture(**{key: wrong})
                with self.assertRaises((AccessError, ValidationError)):
                    check(row)
        row, _, _, order = self.fixture()
        order.shipping_id = "456"
        with self.assertRaises(AccessError):
            check(row)

    def test_denied_record_read_is_never_ignored(self):
        row, _, _, _ = self.fixture()
        row.check_access.side_effect = AccessError("private record")
        with self.assertRaises(AccessError):
            label_scope()(row)


class ExportWizard:
    def __init__(self):
        self.saved = []

    def create(self, values):
        self.saved.append(values)
        return NS(id=55)


def export_method():
    return actual("models/shipment.py", "mercadolibre_shipment_print",
                  "_meli_export_label_batch",
                  {"base64": base64, "zipfile": zipfile, "io": io,
                   "ValidationError": ValidationError})


class TestOwnedExports(unittest.TestCase):
    def test_pdf_export_is_internal_and_contains_exact_bytes(self):
        wizard = ExportWizard()
        url = export_method()(wizard, b"%PDF-1.4\nsynthetic", "pdf", ["101", "102"])
        self.assertTrue(url.startswith("/web/content?model=mercadolibre.shipment.print"))
        self.assertNotIn("mercadolibre.com", url)
        self.assertNotIn(TOKEN, repr(wizard.saved) + url)
        self.assertEqual(base64.b64decode(wizard.saved[0]["print_file"]),
                         b"%PDF-1.4\nsynthetic")
        self.assertEqual(wizard.saved[0]["print_filename"], "Etiquetas_ML_101_102.pdf")

    def test_zpl_zip_is_converted_without_extraction_to_disk(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as writer:
            writer.writestr("1.zpl", "^XA^XZ")
        wizard = ExportWizard()
        export_method()(wizard, buf.getvalue(), "zpl_txt", ["101"])
        self.assertEqual(base64.b64decode(wizard.saved[0]["print_file"]), b"^XA^XZ")
        self.assertEqual(wizard.saved[0]["print_filename"], "Etiquetas_ML_101.zpl")


class FakeRecord:
    def __init__(self, **values):
        self.__dict__.update(values)
        self._fields = set(values)
        self.allowed = True

    def exists(self):
        return self

    def check_access(self, operation):
        if not self.allowed:
            raise AccessError("not readable")


class Lookup:
    def __init__(self, records):
        self.records = records

    def browse(self, rid):
        return self.records.get(rid)


class ImageRequest:
    def __init__(self, *, public=True, published=True, other_image=False,
                 other_company=False, wrong_variant=False, denied=False):
        self.company = object()
        self.template = FakeRecord(company_id=object() if other_company else self.company,
                                   is_published=published, website_id=False)
        self.product = FakeRecord(product_tmpl_id=self.template)
        self.image = FakeRecord(product_tmpl_id=self.template,
                                product_variant_id=FakeRecord() if wrong_variant else False)
        self.image.allowed = not denied
        if other_image:
            self.image.product_tmpl_id = FakeRecord()
        env = {"product.product": Lookup({10: self.product}),
               "product.image": Lookup({22: self.image})}
        self.env = NS(**{"_map": env, "user": NS(_is_public=lambda: public),
                         "companies": [self.company]})
        self.env.__class__.__getitem__ = lambda that, key: that._map[key]
        self.website = None
        self.make_response = lambda content, headers: NS(status_code=200, content=content,
                                                          headers=headers)


def image_method(request):
    ns = {
        "request": request, "AccessError": AccessError, "MissingError": AccessError,
        "Response": lambda body="", status=200, content_type=None: NS(
            status_code=status, content=body),
        "base64": base64, "get_image_full": lambda rec: base64.b64encode(b"synthetic-image"),
        "_get_headers": lambda *args: args,
    }
    return actual("controllers/main.py", "MercadoLibre", "meli_image", ns)


class TestImageAccess(unittest.TestCase):
    def test_public_published_image_and_main_product(self):
        request = ImageRequest()
        method = image_method(request)
        self.assertEqual(method(None, 10).status_code, 200)
        self.assertEqual(method(None, 10, 22).content, b"synthetic-image")

    def test_unpublished_foreign_unrelated_unreadable_and_wrong_variant_denied(self):
        for opts in [
            {"published": False}, {"other_image": True}, {"other_company": True},
            {"wrong_variant": True}, {"denied": True},
        ]:
            with self.subTest(opts=opts):
                request = ImageRequest(**opts)
                self.assertEqual(image_method(request)(None, 10, 22).status_code, 404)

    def test_missing_image_and_product_do_not_disclose(self):
        request = ImageRequest()
        method = image_method(request)
        self.assertEqual(method(None, 10, 999).status_code, 404)
        self.assertEqual(method(None, 99).status_code, 404)


class TestSourceContracts(unittest.TestCase):
    def test_tokenless_visible_routes_and_model_version(self):
        product = (ROOT / "models/product.py").read_text(encoding="utf-8")
        shipment = (ROOT / "models/shipment.py").read_text(encoding="utf-8")
        manifest = (ROOT / "__manifest__.py").read_text(encoding="utf-8")
        self.assertNotIn('ML_permalink_api = str("https://api.mercadolibre.com/items/', product)
        self.assertNotIn('&access_token=', shipment)
        self.assertNotIn("by_token", shipment)
        self.assertNotIn("full_url_link_pdf", shipment)
        self.assertIn("'version': '19.0.26.401'", manifest)


if __name__ == "__main__":
    unittest.main()
