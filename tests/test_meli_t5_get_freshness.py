"""Per-call GET contract: source execution, or real-module binding without a DB.

Standalone uses exact existing method ASTs and the complete admission kernel.
An independent native runner may set REAL_BINDINGS using native_bindings() after
importing the actual addon/SDK under registry, SQL and network fences. It then
runs the SAME TestGetFreshness cases without AST extraction or constructors.
No account, refresh, retry, commercial write or ORM acceptance belongs here.
"""
import ast
from copy import deepcopy
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
REAL_BINDINGS = None
TOKEN = "synthetic-get-token"


class SyntheticApiError(Exception):
    def __init__(self, status=None, reason=None):
        self.status, self.reason, self.body = status, reason, None


class SyntheticRequestError(Exception):
    pass


def native_bindings(util, kernel, orders, validation_error):
    """Bind actual imported classes/functions; do not build SDK/ORM clients."""
    return SimpleNamespace(
        sdk=util.MeliApiSDK, nosdk=util.MeliApiNoSDK,
        api_error=util._ApiException, request_error=util.requests.RequestException,
        kernel=kernel, orders=orders.mercadolibre_orders,
        validation_error=validation_error, mode="native-imported",
    )


def source_bindings():
    """Stdlib-only check; deliberately not presented as native SDK proof."""
    tree = ast.parse((ROOT / "models/meli_util.py").read_text(encoding="utf8"))
    scope = {"_logger": logging.Logger("get-contract"), "urlencode": urlencode,
             "_ApiException": SyntheticApiError,
             "requests": SimpleNamespace(RequestException=SyntheticRequestError)}

    def methods(class_name, names):
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == class_name)
        result = {}
        for name in names:
            method = next(n for n in cls.body
                          if isinstance(n, ast.FunctionDef) and n.name == name)
            exec(compile(ast.Module(body=[method], type_ignores=[]),
                         str(ROOT / "models/meli_util.py"), "exec"), scope)
            result[name] = scope[name]
        return result

    nosdk = type("MeliApiNoSDK", (), methods("MeliApiNoSDK", (
        "get", "json", "_abs_url", "_parse_response")))
    nosdk._benchmark_enabled = False
    scope["MeliApiNoSDK"] = nosdk
    sdk = type("MeliApiSDK", (), methods("MeliApiSDK", ("get", "json")))
    spec = importlib.util.spec_from_file_location(
        "get_contract_admission", ROOT / "models/meli_pack_admission.py")
    kernel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kernel)
    order_scope = {"_logger": logging.Logger("get-orders"),
                   "ValidationError": ValueError}
    order_scope.update(vars(kernel))
    orders = {}
    order_tree = ast.parse((ROOT / "models/orders.py").read_text(encoding="utf8"))
    for name in ("get_billing_info", "_normalize_billing_info_v2",
                 "_meli_collect_pack_commerce"):
        method = next(n for n in ast.walk(order_tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
        method.decorator_list = []
        exec(compile(ast.Module(body=[method], type_ignores=[]),
                     str(ROOT / "models/orders.py"), "exec"), order_scope)
        orders[name] = order_scope[name]
    return SimpleNamespace(sdk=sdk, nosdk=nosdk, api_error=SyntheticApiError,
                           request_error=SyntheticRequestError, kernel=kernel,
                           orders=type("Orders", (), orders),
                           validation_error=ValueError, mode="source-methods")


class ScriptedGet:
    """The resource boundary only; absence of status means no new SDK metadata."""
    def __init__(self, client, actions):
        self.client, self.actions, self.calls = client, list(actions), []

    def __call__(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        action = self.actions.pop(0)
        if "status" in action:
            self.client.api_client.last_response = SimpleNamespace(status=action["status"])
        if "error" in action:
            raise action["error"]
        return deepcopy(action.get("body"))


def order(oid=101):
    return {"id": oid, "seller": {"id": 42}, "pack_id": 900,
            "status": "paid", "currency_id": "USD", "shipping": {"id": 800},
            "order_items": [{"item": {"id": "MLA123", "variation_id": 7},
                             "quantity": 2, "unit_price": 100, "currency_id": "USD"}]}


def association(oid=101):
    return {"order_id": str(oid), "seller_id": 42, "pack_id": "900",
            "item_id": "MLA123", "variation_id": 7, "requested_quantity": 1}


class TestGetFreshness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bindings = REAL_BINDINGS if REAL_BINDINGS is not None else source_bindings()

    def setUp(self):
        fence = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        fence.start()
        self.addCleanup(fence.stop)

    def client(self, actions, api_client=None):
        client = object.__new__(self.bindings.sdk)
        client.api_client = api_client or SimpleNamespace(last_response=None)
        client.response, client.rjson, client.last_status_code = None, {}, None
        client.access_token = TOKEN
        client.resource_get = ScriptedGet(client, actions)
        return client

    def fetch(self, client, path="/orders/101", **kwargs):
        response = client.get(path, {"access_token": TOKEN}, **kwargs)
        self.assertIs(response, client)
        self.assertIs(response.json(), client.rjson)
        return self.bindings.kernel.snapshot_response(response)

    def test_unexpected_and_timeout_never_replay_prior_success(self):
        for error in (RuntimeError("synthetic fault"), TimeoutError("synthetic timeout")):
            with self.subTest(error=type(error).__name__):
                client = self.client([{"status": 200, "body": order()}, {"error": error}])
                first = self.fetch(client)
                body, meta = self.fetch(client)
                self.assertIsNone(meta["status"])
                self.assertFalse(body)
                self.assertIsNone(client.response)
                self.assertIsNone(client.api_client.last_response)
                self.assertEqual(first[0], order())
                self.assertEqual(first[1]["status"], 200)
                self.assertEqual(len(client.resource_get.calls), 2)

    def test_partial_sdk_metadata_then_exception_is_not_a_response(self):
        client = self.client([{"status": 200, "body": order()},
                              {"status": 200, "error": RuntimeError("after metadata")}])
        self.fetch(client)
        body, meta = self.fetch(client)
        self.assertIsNone(meta["status"])
        self.assertFalse(body)
        self.assertIsNone(client.api_client.last_response)

    def test_typed_error_has_only_current_status_and_envelope(self):
        for status in (None, 0, 401, 403, 404, 429, 503):
            with self.subTest(status=status):
                error = self.bindings.api_error(status=status, reason="synthetic denial")
                error.body = "current error body"
                client = self.client([{"status": 200, "body": order()},
                                      {"status": 200, "error": error}])
                self.fetch(client)
                body, meta = self.fetch(client)
                self.assertEqual(meta["status"], status)
                self.assertEqual(body, {"error": "get error", "status": status,
                                        "cause": "synthetic denial", "message": "current error body"})
                self.assertIsNone(client.response)
                self.assertIsNone(client.api_client.last_response)
                self.assertEqual(len(client.resource_get.calls), 2)

    def test_fresh_unknown_status_does_not_inherit_previous_200(self):
        client = self.client([{"status": 200, "body": order()}, {"body": order(102)}])
        self.fetch(client)
        body, meta = self.fetch(client)
        self.assertEqual(body, order(102))
        self.assertIsNone(meta["status"])
        self.assertIsNone(client.api_client.last_response)

    def test_recovery_preserves_new_206_without_retry_or_flattening(self):
        client = self.client([{"status": 200, "body": order()},
                              {"error": TimeoutError("synthetic")},
                              {"status": 206, "body": order(102)}])
        self.fetch(client)
        self.fetch(client)
        body, meta = self.fetch(client)
        self.assertEqual((meta["status"], body), (206, order(102)))
        self.assertEqual(len(client.resource_get.calls), 3)

    def test_reused_api_client_cannot_lend_another_instance_status(self):
        shared = SimpleNamespace(last_response=None)
        first = self.client([{"status": 200, "body": order()}], shared)
        self.fetch(first)
        second = self.client([{"body": order(102)}], shared)
        body, meta = self.fetch(second)
        self.assertEqual(body, order(102))
        self.assertIsNone(meta["status"])
        self.assertEqual(first.last_status_code, 200)

    def test_fresh_empty_scalar_and_list_bodies_are_not_replaced(self):
        for body in (None, {}, [], "", {"error": "fresh"}, [order()]):
            with self.subTest(body=body):
                client = self.client([{"status": 200, "body": order()},
                                      {"status": 200, "body": body}])
                self.fetch(client)
                actual, meta = self.fetch(client)
                self.assertEqual((actual, meta["status"]), (body, 200))

    def test_failed_membership_does_not_admit_a_previous_complete_population(self):
        client = self.client([{"status": 200, "body": [association()]},
                              {"error": RuntimeError("membership unavailable")}])
        self.fetch(client, "/shipments/800/orders")
        with self.assertRaises(self.bindings.kernel.AdmissionError):
            self.bindings.kernel.collect_membership(client, 800, order(), 42, {})
        self.assertEqual(len(client.resource_get.calls), 2)

    def test_individual_206_with_essential_data_is_still_admitted(self):
        client = self.client([{"status": 200, "body": [association(), association(102)]},
                              {"status": 206, "body": order(102)}])
        result = self.bindings.kernel.collect_membership(client, 800, order(), 42, {})
        self.assertEqual([r["status"] for r in result["responses"]], [200, 206])
        self.assertEqual([r["id"] for r in result["orders"]], [101, 102])

    def test_pack_membership_requires_exact_200(self):
        client = self.client([{"status": 206, "body": [association()]}])
        with self.assertRaises(self.bindings.kernel.AdmissionError):
            self.bindings.kernel.collect_membership(client, 800, order(), 42, {})

    def test_actual_pack_collector_refuses_206_before_local_or_member_lookup(self):
        client = self.client([{"status": 206, "body": {"id": 900, "orders": [{"id": 101}]}}])
        receiver = SimpleNamespace(_meli_assert_order_admission_actor=lambda config: "42")
        with self.assertRaises(self.bindings.validation_error):
            self.bindings.orders._meli_collect_pack_commerce(receiver, order(), object(), client)
        self.assertEqual(len(client.resource_get.calls), 1)

    def test_fresh_unknown_http_legacy_billing_stays_compatible(self):
        legacy = {"billing_info": {"doc_number": "synthetic legacy"}}
        client = self.client([{"status": 200, "body": order()}, {"body": legacy}])
        self.fetch(client)
        receiver = SimpleNamespace()
        data = {"id": 101, "site_id": "MLA", "buyer": {"billing_info": {}}}
        result = self.bindings.orders.get_billing_info(receiver, meli=client, data=data)
        self.assertEqual(result, legacy["billing_info"])
        self.assertIsNone(client.last_status_code)
        self.assertEqual(len(client.resource_get.calls), 2)

    def test_billing_stale_guard_is_preserved_after_swallowed_failure(self):
        client = self.client([{"status": 200, "body": {"billing_info": {"doc_number": "old"}}},
                              {"error": TimeoutError("synthetic")}])
        self.fetch(client)
        data = {"id": 101, "site_id": "MLA", "buyer": {"billing_info": {}}}
        result = self.bindings.orders.get_billing_info(SimpleNamespace(), meli=client, data=data)
        self.assertEqual(result, {})

    def test_header_delegation_starts_without_old_sdk_outcome(self):
        client = self.client([{"status": 200, "body": order()}])
        self.fetch(client)
        seen = []

        def mini(path, params, extra_headers=None):
            seen.append((client.last_status_code, client.rjson, client.api_client.last_response))
            self.assertEqual(extra_headers, {"x-version": "2"})
            client.response = client.rjson = {"fresh": "delegated"}
            client.last_status_code = 206
            return client

        client.get_mini = mini
        body, meta = self.fetch(client, extra_headers={"x-version": "2"}, ignored_kw=True)
        self.assertEqual(seen, [(None, {}, None)])
        self.assertEqual((body, meta["status"]), ({"fresh": "delegated"}, 206))
        self.assertEqual(len(client.resource_get.calls), 1)

    def test_pre_transport_failure_does_not_replay_old_success(self):
        client = self.client([{"status": 200, "body": order()}])
        self.fetch(client)
        self.assertIs(client.get("/orders/102", None), client)
        body, meta = self.bindings.kernel.snapshot_response(client)
        self.assertIsNone(meta["status"])
        self.assertFalse(body)
        self.assertEqual(len(client.resource_get.calls), 1)

    def test_nosdk_default_transport_contract_is_unchanged(self):
        bindings = self.bindings
        client = object.__new__(bindings.nosdk)
        client.base_url, client.access_token, client.seller_id = "https://synthetic.invalid", TOKEN, "42"
        calls = []
        response = SimpleNamespace(status_code=200, text="", json=lambda: order())
        replies = [response, bindings.request_error("synthetic timeout")]

        def get(url, **kwargs):
            calls.append((url, kwargs))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        client._session = SimpleNamespace(get=get)
        self.assertEqual(self.fetch(client)[1]["status"], 200)
        body, meta = self.fetch(client)
        self.assertEqual((meta["status"], body["cause"]), (0, "request_exception"))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
