"""Source-bound image upload contracts; no Odoo, SDK, requests or network needed.

The real backend and product methods are compiled from their AST, not copied.
HTTP is intercepted before transmission; wire multipart encoding and real ORM
integration require the unavailable dependencies and are not certified here.
"""
import ast
import base64
from copy import deepcopy
import hashlib
import io
import logging
from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "SYNTHETIC-C5-token/+?=safe"
JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9"
PICTURE = {"id": "PIC-1", "max_size": "100x80", "other": {"kept": True},
           "variations": [{"url": "https://synthetic.invalid/image.jpg",
                           "secure_url": "https://synthetic.invalid/image.jpg",
                           "size": "100x80"}]}


def nodes(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def method(tree, name, owner=None):
    scope = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                 and n.name == owner) if owner else tree
    matches = [n for n in ast.walk(scope) if isinstance(n, ast.FunctionDef)
               and n.name == name]
    if len(matches) != 1:
        raise AssertionError("exact source method required: " + name)
    node = deepcopy(matches[0])
    node.decorator_list = []
    return node


class RequestError(Exception):
    pass


class Response:
    def __init__(self, payload=PICTURE, status=200, malformed=False, text=""):
        self.payload = deepcopy(payload)
        self.status_code = status
        self.malformed = malformed
        self.text = text
        self.url = "https://synthetic.invalid/pictures"

    def json(self):
        if self.malformed:
            raise ValueError("invalid JSON")
        return self.payload


class Transport:
    def __init__(self, response=None, error=None):
        self.response = response or Response()
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        # Match requests' actual query assembly without contacting a host.
        params = kwargs.get("params")
        query = params if isinstance(params, str) else urlencode(params or {})
        effective_url = url + (("&" if "?" in url else "?") + query if query else "")
        self.calls.append((effective_url, kwargs))
        if self.error:
            raise self.error
        return self.response


def backend(kind, transport, logger):
    path = ROOT / "models/meli_util.py"
    tree = nodes(path)
    names = ["upload", "uploadfiles", "json"]
    if kind == "MeliApiNoSDK":
        names += ["_abs_url", "_parse_response", "need_login"]
    else:
        names += ["need_login"]
    namespace = {"requests": SimpleNamespace(post=transport.post,
                 RequestException=RequestError), "_logger": logger,
                 "configuration_sdk": SimpleNamespace(host="https://synthetic.invalid"),
                 "urlencode": urlencode}
    # Include only upload-local helpers if present, plus existing redaction.
    helpers = [deepcopy(n) for n in tree.body if isinstance(n, ast.FunctionDef)
               and (n.name.startswith("_meli_upload_") or n.name == "meli_redact")]
    body = [method(tree, name, kind) for name in names]
    cls = ast.ClassDef(name=kind, bases=[], keywords=[], body=body, decorator_list=[])
    code = compile(ast.fix_missing_locations(ast.Module(body=helpers + [cls],
                   type_ignores=[])), str(path), "exec")
    exec(code, namespace)
    client = namespace[kind]()
    client.base_url = "https://synthetic.invalid"
    client._session = transport
    client.access_token = TOKEN
    client.needlogin_state = False
    client.response = None
    client.rjson = {"old": "payload"}
    return client


def product_methods(logger):
    path = ROOT / "models/product.py"
    tree = nodes(path)
    names = ["_collect_and_upload_images_for_meli", "product_meli_upload_image",
             "_meli_upload_image", "product_meli_upload_multi_images"]
    body = [method(tree, name) for name in names]
    cls = ast.ClassDef(name="Product", bases=[], keywords=[], body=body, decorator_list=[])
    namespace = {"base64": base64, "hashlib": hashlib, "_logger": logger,
                 "get_first_image_to_publish": lambda p: p.image_1920,
                 "get_image_full": lambda p: p.image_1920,
                 "variant_image_ids": lambda p: p.variant_images,
                 "template_image_ids": lambda p: p.template_images}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 str(path), "exec"), namespace)
    product = namespace["Product"]()
    product.id = 10
    product.image_1920 = base64.b64encode(JPEG)
    product.product_variant_ids = []
    product.variant_images = []
    product.template_images = []
    product.meli_id = "ITEM-1"
    product.writes = []
    product.write = lambda values: product.writes.append(values)
    product.ensure_one = lambda: None
    config = {"mercadolibre_banner": False}
    config = type("Config", (dict,), {})(config)
    config.mercadolibre_do_not_use_first_image = False
    config.mercadolibre_banner = False
    company = config
    product.env = {"product.product": object()}
    product.env = type("Env", (dict,), {})(product.env)
    product.env.user = SimpleNamespace(company_id=company)
    return product, config


class TestImageUploadSecurity(unittest.TestCase):
    def setUp(self):
        self.socket_guard = patch.object(socket, "socket", side_effect=AssertionError("network forbidden"))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.logs = io.StringIO()
        self.logger = logging.Logger("c5-contract", logging.DEBUG)
        self.logger.addHandler(logging.StreamHandler(self.logs))

    def clients(self, response=None, error=None):
        for kind in ("MeliApiNoSDK", "MeliApiSDK"):
            transport = Transport(response, error)
            yield kind, backend(kind, transport, self.logger), transport

    def assert_secure(self, transport, files, token=TOKEN):
        self.assertEqual(len(transport.calls), 1)
        url, kwargs = transport.calls[0]
        self.assertNotIn("access_token", url)
        self.assertNotIn(token, url)
        self.assertNotIn(token, str(kwargs.get("params", {})))
        self.assertNotIn("access_token", parse_qs(urlsplit(url).query))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + token)
        self.assertIs(kwargs["files"], files)
        self.assertNotIn("data", kwargs)
        self.assertNotIn("json", kwargs)
        self.assertNotIn("Content-Type", kwargs["headers"])
        other = {k: v for k, v in kwargs.items() if k != "headers"}
        self.assertNotIn(token, str(other))

    def test_jpeg_upload_security_and_complete_return_contract(self):
        for kind, client, transport in self.clients():
            with self.subTest(kind=kind):
                files = {"file": ("image.jpg", JPEG, "image/jpeg")}
                params = {"access_token": TOKEN, "timeout": 7}
                previous = params.copy()
                self.assertIs(client.upload("/pictures", files, params), client)
                self.assert_secure(transport, files)
                self.assertEqual(client.json(), PICTURE)
                self.assertEqual(params, previous)
                if kind == "MeliApiNoSDK":
                    self.assertIs(client.response, client.rjson)
                    self.assertEqual(transport.calls[0][1]["timeout"], 7)
                else:
                    self.assertIs(client.response, transport.response)
                    self.assertNotIn("timeout", transport.calls[0][1])

    def test_uploadfiles_is_the_same_secure_public_contract(self):
        for kind, client, transport in self.clients():
            with self.subTest(kind=kind):
                files = {"file": ("custom.jpeg", JPEG, "image/jpeg")}
                self.assertIs(client.uploadfiles("/pictures", files, {"access_token": TOKEN}), client)
                self.assert_secure(transport, files)
                self.assertEqual(client.json(), PICTURE)

    def test_upload_delegates_without_altering_arguments_or_return(self):
        for kind, client, _ in self.clients():
            with self.subTest(kind=kind):
                files, params, sentinel = {}, {"access_token": TOKEN}, object()
                with patch.object(client, "uploadfiles", return_value=sentinel) as delegated:
                    self.assertIs(client.upload("/pictures", files, params), sentinel)
                delegated.assert_called_once_with("/pictures", files, params)

    def test_http_error_payloads_are_not_rewritten_or_hidden(self):
        for status in (400, 401, 403, 429, 500, 503):
            payload = {"error": "http_error", "status": status, "message": "rejected"}
            for kind, client, transport in self.clients(Response(payload, status)):
                with self.subTest(kind=kind, status=status):
                    files = {"file": ("image.jpg", JPEG, "image/jpeg")}
                    client.upload("/pictures", files, {"access_token": TOKEN})
                    self.assert_secure(transport, files)
                    self.assertEqual(client.json(), payload)

    def test_incomplete_json_is_preserved_for_existing_consumers(self):
        for payload in ({}, {"id": "PIC-1"}, None, [], "incomplete"):
            for kind, client, _ in self.clients(Response(payload)):
                with self.subTest(kind=kind, payload=payload):
                    client.upload("/pictures", {}, {"access_token": TOKEN})
                    self.assertEqual(client.json(), payload)

    def test_malformed_and_empty_response_preserves_backend_semantics(self):
        for status, text in ((200, "bad-json"), (400, ""), (503, "")):
            for kind, client, transport in self.clients(Response(status=status, malformed=True, text=text)):
                with self.subTest(kind=kind, status=status):
                    client.upload("/pictures", {}, {"access_token": TOKEN})
                    if kind == "MeliApiSDK":
                        self.assertEqual(client.json(), {"error": "invalid JSON"})
                        self.assertIs(client.response, transport.response)
                    elif text:
                        self.assertEqual(client.json(), text)
                    else:
                        self.assertEqual(client.json()["error"], "http_%s" % status)
                    self.assertNotIn(TOKEN, self.logs.getvalue())

    def test_transport_errors_redact_raw_and_encoded_tokens_in_both_entrypoints(self):
        for token in (TOKEN, "tiny"):
            variants = (token, quote(token, safe=""), quote_plus(token))
            for error_type in (RequestError, type("Timeout", (RequestError,), {})):
                for entrypoint in ("upload", "uploadfiles"):
                    message = "transport failed " + " | ".join(variants)
                    for kind, client, transport in self.clients(error=error_type(message)):
                        with self.subTest(kind=kind, entrypoint=entrypoint, token=token, error=error_type):
                            getattr(client, entrypoint)("/pictures", {}, {"access_token": token})
                            self.assertEqual(set(client.json()), {"error"})
                            self.assertIn("transport failed", client.json()["error"])
                            for secret in set(variants):
                                self.assertNotIn(secret, client.json()["error"])
                                self.assertNotIn(secret, self.logs.getvalue())
                            if kind == "MeliApiNoSDK":
                                self.assertIs(client.response, client.rjson)

    def test_reflected_http_error_payload_and_real_consumer_log_are_redacted(self):
        for token in (TOKEN, "tiny"):
            payload = {"error": "forbidden", "status": 401,
                       "message": "Authorization rejected Bearer " + token,
                       "cause": [{"code": "auth", "details": quote(token, safe="")}],
                       "metadata": {"retry": False, "description": "keep me"}}
            for entrypoint in ("upload", "uploadfiles"):
                for kind, client, transport in self.clients(Response(payload, 401)):
                    with self.subTest(kind=kind, token=token, entrypoint=entrypoint):
                        client.access_token = token
                        getattr(client, entrypoint)("/pictures", {}, {"access_token": token})
                        product, config = product_methods(self.logger)
                        result = product._collect_and_upload_images_for_meli(meli=client, config=config)
                        self.assertEqual(result["all_pic_ids"], [])
                        self.assertIn("MELI Image upload error", self.logs.getvalue())
                        self.assertNotIn(token, self.logs.getvalue())
                        expected = deepcopy(payload)
                        expected["message"] = "Authorization rejected Bearer ***"
                        expected["cause"][0]["details"] = "***"
                        self.assertEqual(client.json(), expected)
                        self.assertEqual(transport.response.payload, payload)
                        if kind == "MeliApiNoSDK":
                            self.assertIs(client.response, client.rjson)
                        else:
                            self.assertIs(client.response, transport.response)
                        for secret in {token, quote(token, safe="")}:
                            self.assertNotIn(secret, str(client.json()))
                            self.assertNotIn(secret, self.logs.getvalue())

    def test_non_json_diagnostic_text_redacts_without_changing_error_semantics(self):
        for kind, client, transport in self.clients(Response(status=500, malformed=True,
                text="error: Bearer " + TOKEN + " | " + quote_plus(TOKEN))):
            with self.subTest(kind=kind):
                client.upload("/pictures", {}, {"access_token": TOKEN})
                if kind == "MeliApiNoSDK":
                    self.assertEqual(client.json(), "error: Bearer *** | ***")
                    self.assertIs(client.response, client.rjson)
                    product, config = product_methods(self.logger)
                    product._collect_and_upload_images_for_meli(meli=client, config=config)
                else:
                    self.assertEqual(client.json(), {"error": "invalid JSON"})
                    self.assertIs(client.response, transport.response)
                self.assertNotIn(TOKEN, self.logs.getvalue())

    def test_non_sensitive_transport_error_is_preserved(self):
        for kind, client, _ in self.clients(error=RequestError("connection refused")):
            with self.subTest(kind=kind):
                self.assertIs(client.upload("/pictures", {}, {"access_token": TOKEN}), client)
                self.assertEqual(client.json(), {"error": "connection refused"})

    def test_sdk_json_decode_diagnostic_redacts_token(self):
        response = Response()
        response.json = lambda: (_ for _ in ()).throw(ValueError("invalid JSON " + TOKEN))
        transport = Transport(response)
        client = backend("MeliApiSDK", transport, self.logger)
        client.upload("/pictures", {}, {"access_token": TOKEN})
        self.assertEqual(client.json(), {"error": "invalid JSON ***"})
        self.assertIs(client.response, response)

    def test_sdk_invalid_params_preserves_caught_error(self):
        transport = Transport()
        client = backend("MeliApiSDK", transport, self.logger)
        client.upload("/pictures", {}, None)
        self.assertIn("error", client.json())
        self.assertEqual(transport.calls, [])

    def test_missing_token_does_not_generate_query_credentials(self):
        for kind, client, transport in self.clients():
            with self.subTest(kind=kind):
                client.upload("/pictures", {}, {})
                self.assertNotIn("access_token", transport.calls[0][0])

    def test_all_three_real_product_upload_methods(self):
        for kind, client, transport in self.clients():
            with self.subTest(kind=kind):
                product, config = product_methods(self.logger)
                collected = product._collect_and_upload_images_for_meli(meli=client, config=config)
                self.assertEqual(collected["all_pic_ids"], ["PIC-1"])
                self.assertEqual(collected["template_pic_ids"], ["PIC-1"])
                self.assertEqual(product.product_meli_upload_image(meli=client, config=config), "PIC-1")
                image = SimpleNamespace(image_1920=product.image_1920)
                self.assertEqual(product._meli_upload_image(image, meli=client, config=config), [{"id": "PIC-1"}])
                self.assertEqual(image.meli_imagen_link, PICTURE["variations"][0]["secure_url"])
                self.assertEqual(image.meli_imagen_hash, hashlib.blake2b(JPEG).hexdigest())
                self.assertEqual(len(transport.calls), 3)
                for call in transport.calls:
                    single = SimpleNamespace(calls=[call])
                    self.assert_secure(single, call[1]["files"])
                    self.assertEqual(call[1]["files"], {"file": ("image.jpg", JPEG, "image/jpeg")})

    def test_real_product_consumers_preserve_http_error(self):
        payload = {"error": "forbidden", "status": 403, "message": "denied"}
        for kind, client, transport in self.clients(Response(payload, 403)):
            with self.subTest(kind=kind):
                product, config = product_methods(self.logger)
                image = SimpleNamespace(image_1920=product.image_1920)
                self.assertEqual(product.product_meli_upload_image(meli=client, config=config), payload)
                self.assertEqual(product._meli_upload_image(image, meli=client, config=config), payload)
                result = product._collect_and_upload_images_for_meli(meli=client, config=config)
                self.assertEqual(result["all_pic_ids"], [])
                self.assertEqual(product.writes, [])
                self.assertFalse(hasattr(image, "meli_imagen_id"))
                self.assertNotIn(TOKEN, self.logs.getvalue())

    def test_real_multi_image_variant_template_banner_consumers(self):
        for kind, client, transport in self.clients():
            with self.subTest(kind=kind):
                product, config = product_methods(self.logger)
                images = [SimpleNamespace(image_1920=product.image_1920) for _ in range(3)]
                product.variant_images, product.template_images = images[:1], images[1:2]
                config["mercadolibre_banner"] = type("Banner", (dict,), {})(images_id=images[2:])
                config["mercadolibre_banner"].images_id = images[2:]
                config.mercadolibre_banner = config["mercadolibre_banner"]
                result = product.product_meli_upload_multi_images(meli=client, config=config)
                self.assertEqual(result, [{"id": "PIC-1"}] * 3)
                self.assertEqual(len(transport.calls), 3)
                for image in images:
                    self.assertEqual(image.meli_imagen_id, "PIC-1")

    def test_actual_consumer_inventory_has_no_uncovered_upload_call(self):
        calls = []
        for path in ROOT.rglob("*.py"):
            if "tests" in path.parts or "melisdk" in path.parts:
                continue
            for node in ast.walk(nodes(path)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("upload", "uploadfiles"):
                    if not (path.name == "meli_util.py" and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"):
                        calls.append((path.relative_to(ROOT).as_posix(), node.lineno, node.func.attr))
        self.assertEqual(sorted(calls), [("models/product.py", 573, "upload"),
                                ("models/product.py", 3204, "upload"),
                                ("models/product.py", 3311, "upload")])


if __name__ == "__main__":
    unittest.main()
