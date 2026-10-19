"""Source-bound Billing Info tests: stdlib only, no Odoo/HTTP/DB proof.

Run: python -B -m unittest discover -s tests -p test_billing_info.py -v.
Execute the real order methods and NoSDK/SDK GET methods with synthetic sessions.
"""
import ast
from copy import deepcopy
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-billing-token"
ORDER = {"id": 123, "site_id": "MLA", "buyer": {"billing_info": {"id": 456}}}
INFO = {"name": "Fixture", "identification": {"type": "DNI", "number": "123"}}
NEW = {"buyer": {"billing_info": INFO}}
LEGACY = {"billing_info": {"doc_number": "legacy"}}


def methods(path, names, scope):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for name in names:
        matches = [n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(matches) != 1:
            raise AssertionError("one real method required: " + name)
        exec(compile(ast.Module(body=[matches[0]], type_ignores=[]),
                     str(path), "exec"), scope)
    return {name: scope[name] for name in names}


class RequestError(Exception):
    pass


class Reply:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = deepcopy(payload)
        self.text = ""

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, deepcopy(kwargs)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def clients(session):
    """Load exact backend methods; construction/HTTP transport are explicit fakes."""
    tree = ast.parse((ROOT / "models/meli_util.py").read_text(encoding="utf-8"))
    scope = {"_logger": logging.Logger("billing-backend"), "urlencode": urlencode,
             "requests": SimpleNamespace(RequestException=RequestError),
             "configuration_nosdk": object(), "_ApiException": RequestError}

    def load(class_name, names):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == class_name)
        for name in names:
            method = next(n for n in node.body
                          if isinstance(n, ast.FunctionDef) and n.name == name)
            exec(compile(ast.Module(body=[method], type_ignores=[]),
                         str(ROOT / "models/meli_util.py"), "exec"), scope)
        return {name: scope[name] for name in names}

    def initialize(self, config=None):
        self._session = session
        self.base_url = "https://synthetic.invalid"
        self.access_token = TOKEN
        self.seller_id = "fixture"
        self.last_status_code = None
        self.rjson = None

    nosdk = type("MeliApiNoSDK", (), dict(load("MeliApiNoSDK", (
        "get", "json", "_abs_url", "_parse_response")),
        __init__=initialize, _benchmark_enabled=False))
    scope["MeliApiNoSDK"] = nosdk
    sdk = type("MeliApiSDK", (), dict(load("MeliApiSDK", ("get", "get_mini", "json")),
        __init__=initialize))
    return nosdk, sdk


class TestBillingInfo(unittest.TestCase):
    def setUp(self):
        # Prevent accidental network if a fixture is replaced by real transport.
        self.network = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        scope = {"_logger": logging.Logger("billing-order")}
        self.model = type("Orders", (), methods(ROOT / "models/orders.py", (
            "get_billing_info", "_normalize_billing_info_v2"), scope))()

    def fetch(self, replies, backend="nosdk", data=None):
        session = Session(replies)
        nosdk, sdk = clients(session)
        client = (nosdk if backend == "nosdk" else sdk)()
        result = self.model.get_billing_info(meli=client, data=deepcopy(
            ORDER if data is None else data))
        return result, session, client

    def test_new_200_nosdk_and_sdk_header_delegation_use_one_get(self):
        for backend in ("nosdk", "sdk"):
            with self.subTest(backend=backend):
                result, session, client = self.fetch([Reply(200, NEW)], backend)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(result["DOC_NUMBER"], "123")
                self.assertEqual(result["FIRST_NAME"], "Fixture")
                self.assertTrue(session.calls[0][0].endswith("/orders/billing-info/MLA/456"))
                self.assertEqual(session.calls[0][1]["headers"]["x-version"], "2")
                self.assertEqual(client.last_status_code, 200)

    def test_root_wrapper_and_api_site_preserve_real_normalization(self):
        result, session, _ = self.fetch([Reply(200, {"billing_info": INFO, "site_id": "MLM"})])
        self.assertEqual(result["DOC_NUMBER"], "123")
        self.assertEqual(result["INVOICE_TYPE"], "")
        self.assertEqual(len(session.calls), 1)

    def test_conventional_response_status_without_client_status(self):
        client = SimpleNamespace(access_token=TOKEN, last_status_code=503)
        client.get = lambda *a, **kw: Reply(200, NEW)
        result = self.model.get_billing_info(meli=client, data=deepcopy(ORDER))
        self.assertEqual(result["DOC_NUMBER"], "123")

    def test_known_errors_never_accept_billing_shaped_bodies(self):
        for status in (0, 201, 204, 400, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                result, session, _ = self.fetch([Reply(status, NEW), Reply(status, LEGACY)])
                self.assertEqual(result, {"id": 456})
                self.assertEqual(len(session.calls), 2)

    def test_error_envelopes_do_not_become_success_even_with_200(self):
        result, _, _ = self.fetch([Reply(200, dict(NEW, error="failed")),
                                  Reply(200, dict(LEGACY, error="failed"))])
        self.assertEqual(result, {"id": 456})

    def test_incomplete_responses_preserve_order_fallback(self):
        for payload in (None, {}, [], "text", {"buyer": []},
                        {"buyer": {"billing_info": []}}, {"billing_info": "text"},
                        {"billing_info": {}}, ValueError("invalid JSON")):
            with self.subTest(payload=payload):
                result, _, _ = self.fetch([Reply(200, payload), Reply(200, payload)])
                self.assertEqual(result, {"id": 456})

    def test_timeout_backend_envelope_and_raised_timeout_are_not_success(self):
        result, _, client = self.fetch([RequestError("timeout"), RequestError("timeout")])
        self.assertEqual(result, {"id": 456})
        self.assertEqual(client.last_status_code, 0)
        client = SimpleNamespace(access_token=TOKEN)
        client.get = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("timeout"))
        self.assertEqual(self.model.get_billing_info(meli=client, data=deepcopy(ORDER)), {"id": 456})

    def test_missing_billing_id_keeps_legacy_and_additional_info(self):
        data = deepcopy(ORDER)
        data["buyer"]["billing_info"] = {}
        legacy = {"billing_info": {"additional_info": [{"type": "DOC_NUMBER", "value": "123"}]}}
        result, session, _ = self.fetch([Reply(200, legacy)], data=data)
        self.assertEqual(result["DOC_NUMBER"], "123")
        self.assertEqual(result["doc_number"], "123")
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0][0].endswith("/orders/123/billing_info"))

    def test_missing_fiscal_identity_stays_missing_without_policy_change(self):
        result, session, _ = self.fetch([Reply(200, {"billing_info": {"name": "Fixture"}})])
        self.assertEqual(result["DOC_NUMBER"], "")
        self.assertEqual(result["DOC_TYPE"], "")
        self.assertEqual(result["INVOICE_TYPE"], "")
        self.assertEqual(len(session.calls), 1)

    def test_unknown_new_status_is_not_inferred_as_200(self):
        replies = [Reply(None, NEW), Reply(404, {})]
        client = SimpleNamespace(access_token=TOKEN)
        client.get = lambda *a, **kw: replies.pop(0)
        self.assertEqual(self.model.get_billing_info(meli=client, data=deepcopy(ORDER)), {"id": 456})
        self.assertEqual(replies, [])

    def test_last_status_takes_precedence_and_none_uses_response_status(self):
        for status, expected in ((403, {"id": 456}), (None, "123")):
            with self.subTest(status=status):
                response = Reply(200, NEW)
                response.last_status_code = status
                client = SimpleNamespace(access_token=TOKEN, get=lambda *a, **kw: response)
                result = self.model.get_billing_info(meli=client, data=deepcopy(ORDER))
                self.assertEqual(result if status else result["DOC_NUMBER"], expected)

    def test_failed_normalization_or_flattening_never_returns_partial_success(self):
        malformed = {"billing_info": {"name": "Fixture", "identification": "invalid"}}
        result, _, _ = self.fetch([Reply(200, malformed), Reply(404, {})])
        self.assertEqual(result, {"id": 456})
        result, _, _ = self.fetch([Reply(404, {}), Reply(200, {
            "billing_info": {"additional_info": [{"type": "DOC_NUMBER"}]}})])
        self.assertEqual(result, {"id": 456})

    def test_sdk_legacy_unknown_status_with_fresh_payload_remains_compatible(self):
        session = Session([])
        _, sdk = clients(session)
        client = sdk()
        client.api_client = SimpleNamespace(last_response=None)
        client.resource_get = lambda **kw: deepcopy(LEGACY)
        data = deepcopy(ORDER)
        data["buyer"]["billing_info"] = {}
        result = self.model.get_billing_info(meli=client, data=data)
        self.assertEqual(result, LEGACY["billing_info"])
        self.assertIsNone(client.last_status_code)

    def test_sdk_swallowed_exception_cannot_replay_stale_legacy_success(self):
        session = Session([])
        _, sdk = clients(session)
        client = sdk()
        client.rjson = deepcopy(LEGACY)
        client.last_status_code = 200
        client.resource_get = lambda **kw: (_ for _ in ()).throw(TimeoutError("timeout"))
        data = deepcopy(ORDER)
        data["buyer"]["billing_info"] = {}
        self.assertEqual(self.model.get_billing_info(meli=client, data=data), {})

    def test_mutable_response_is_snapshotted_before_next_get_and_not_modified(self):
        class Mutable:
            access_token = TOKEN
            last_status_code = None
            rjson = None

            def __init__(self):
                self.events = []

            def get(self, path, *args, **kwargs):
                self.events.append("get")
                if len(self.events) == 1:
                    self.last_status_code, self.rjson = 404, deepcopy(NEW)
                else:
                    self.last_status_code = 200
                    self.rjson.clear()
                    self.rjson = {"billing_info": {"additional_info": [
                        {"type": "DOC_NUMBER", "value": "456"}]}}
                return self

            def json(self):
                self.events.append("json")
                return self.rjson

        client = Mutable()
        snapshots = []

        def snapshot(payload):
            copied = deepcopy(payload)
            snapshots.append(copied)
            return copied

        with patch("copy.deepcopy", side_effect=snapshot):
            result = self.model.get_billing_info(meli=client, data=deepcopy(ORDER))
        self.assertEqual(client.events, ["get", "json", "get", "json"])
        self.assertEqual(snapshots[0], NEW)
        self.assertEqual(result["DOC_NUMBER"], "456")
        self.assertNotIn("DOC_NUMBER", client.rjson["billing_info"])

    def test_no_client_preserves_supplied_order_billing(self):
        self.assertEqual(self.model.get_billing_info(data=deepcopy(ORDER)), {"id": 456})

    def test_cuit_missing_or_blank_taxpayer_never_invents_invoice_type(self):
        cases = (
            ("missing_taxes", {}), ("null_taxes", {"taxes": None}),
            ("missing_taxpayer", {"taxes": {}}),
            ("null_taxpayer", {"taxes": {"taxpayer_type": None}}),
            ("missing_description", {"taxes": {"taxpayer_type": {}}}),
            ("null_description", {"taxes": {"taxpayer_type": {"description": None}}}),
            ("empty_description", {"taxes": {"taxpayer_type": {"description": ""}}}),
            ("blank_description", {"taxes": {"taxpayer_type": {"description": " \t\n "}}}),
        )
        for name, taxes in cases:
            with self.subTest(case=name):
                body = {"id": 789, "name": "Synthetic CUIT buyer",
                        "identification": {"type": "CUIT", "number": "30712345671"}, **taxes}
                before = deepcopy(body)
                result = self.model._normalize_billing_info_v2(body, site_id="MLA")
                self.assertEqual(body, before)
                self.assertEqual(result["id"], 789)
                self.assertEqual(result["identification"], before["identification"])
                self.assertEqual(result["DOC_NUMBER"], "30712345671")
                self.assertEqual(result["DOC_TYPE"], "CUIT")
                description = ((body.get("taxes") or {}).get("taxpayer_type") or {}).get("description")
                self.assertEqual(result["TAXPAYER_TYPE_ID"], description or "")
                for field in taxes:
                    self.assertEqual(result[field], before[field])
                self.assertEqual(result["INVOICE_TYPE"], "")

    def test_cuit_unknown_nonempty_taxpayer_keeps_unknown_label_and_input(self):
        body = {"identification": {"type": "CUIT", "number": "30712345671"},
                "taxes": {"taxpayer_type": {"description": "Synthetic unclassified taxpayer"}}}
        before = deepcopy(body)
        result = self.model._normalize_billing_info_v2(body, site_id="MLA")
        self.assertEqual(result["INVOICE_TYPE"], "")
        self.assertEqual(result["TAXPAYER_TYPE_ID"], "Synthetic unclassified taxpayer")
        self.assertEqual(body, before)

    def test_explicit_invoice_type_values_and_attribute_precedence_stay_exact(self):
        for attribute, root, expected in (
                ("Factura A", None, "Factura A"),
                ("REMOTE_UNKNOWN_LABEL", "Factura B", "REMOTE_UNKNOWN_LABEL"),
                ("Factura C", "Factura A", "Factura C"),
                (None, "Factura B", "Factura B"),
                ("", "ROOT_UNKNOWN_LABEL", "ROOT_UNKNOWN_LABEL")):
            with self.subTest(attribute=attribute, root=root):
                body = {"identification": {"type": "CUIT", "number": "30712345671"},
                        "attributes": {"invoice_type": attribute}, "invoice_type": root}
                before = deepcopy(body)
                result = self.model._normalize_billing_info_v2(body, site_id="MLA")
                self.assertEqual(result["INVOICE_TYPE"], expected)
                self.assertEqual(body, before)

    def test_recognized_taxpayer_document_and_non_mla_controls_stay_unchanged(self):
        cases = [("CUIT", "Responsable Inscripto", "MLA", "Factura A")]
        cases += [("CUIT", value, "MLA", "Factura B") for value in (
            "Monotributo", "Exento", "Consumidor Final", "No Responsable", "No Categorizado",
            "Responsable Inscripto - Monotributo")]
        cases += [(doc, "", "MLA", "Factura B") for doc in ("DNI", "CUIL")]
        cases += [("CUIT", "Responsable Inscripto", site, "") for site in (None, "MLM")]
        for document, description, site, expected in cases:
            with self.subTest(document=document, description=description, site=site):
                body = {"identification": {"type": document, "number": "30712345671"},
                        "taxes": {"taxpayer_type": {"description": description}}}
                before = deepcopy(body)
                result = self.model._normalize_billing_info_v2(body, site_id=site)
                self.assertEqual(result["INVOICE_TYPE"], expected)
                self.assertEqual(body, before)

    def test_fresh_200_cuit_missing_taxpayer_fetch_preserves_unknown_and_payload(self):
        for backend in ("nosdk", "sdk"):
            with self.subTest(backend=backend):
                body = {"buyer": {"billing_info": {"id": 789,
                    "identification": {"type": "CUIT", "number": "30712345671"}}}}
                reply = Reply(200, body)
                before = deepcopy(reply.payload)
                data = deepcopy(ORDER)
                original_order = deepcopy(data)
                session = Session([reply])
                nosdk, sdk = clients(session)
                client = (nosdk if backend == "nosdk" else sdk)()
                result = self.model.get_billing_info(meli=client, data=data)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(client.last_status_code, 200)
                self.assertEqual(data, original_order)
                self.assertEqual(reply.payload, before)
                self.assertEqual((result["id"], result["DOC_TYPE"], result["DOC_NUMBER"]),
                                 (789, "CUIT", "30712345671"))
                self.assertEqual(result["TAXPAYER_TYPE_ID"], "")
                self.assertEqual(result["INVOICE_TYPE"], "")


if __name__ == "__main__":
    unittest.main()
