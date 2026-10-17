"""Execute actual C6 methods with deterministic transport/model doubles.

No Odoo/database/network claim: registered ORM/PostgreSQL tests are separate.
"""
import ast
import copy
import datetime
import math
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOG = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                      error=lambda *a, **k: None)


class NetworkError(Exception):
    pass


class ApiError(Exception):
    status = 503
    reason = "synthetic"
    body = "synthetic"


def actual(path, name, owner=None, extra=None):
    tree = ast.parse((ROOT / path).read_bytes())
    nodes = ast.walk(tree)
    if owner:
        cls = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == owner)
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    else:
        node = next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    scope = {"_logger": LOG, "math": math, "datetime": datetime.datetime,
             "ml_datetime": lambda value: value, "meli_message_post": lambda *a: None,
             "_ApiException": ApiError, "requests": SimpleNamespace(RequestException=NetworkError)}
    scope.update(extra or {})
    exec(compile(ast.fix_missing_locations(ast.Module([node], [])), "actual-c6-method", "exec"), scope)
    return scope[name]


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = copy.deepcopy(body), status
    def json(self):
        return copy.deepcopy(self.body)
    def __bool__(self):
        return self.status_code < 400


class StockClient:
    client_id = "synthetic"
    access_token = None
    seller_id = "42"
    def __init__(self, result=None, status=200):
        self.item = {"id": "MLA123", "seller_id": 42, "tags": [], "variations": [],
                     "available_quantity": 1, "user_product_id": None, "inventory_id": None,
                     "shipping": {"logistic_type": "drop_off"}}
        # Captured successful existing identity-probe response, not an auth flag.
        self._meli_stock_seller_snapshot = {"status": 200, "seller_id": "42",
            "company_id": 1, "client_id": "synthetic", "body": {"id": 42, "tags": []}}
        self.result = {"id": "MLA123", "available_quantity": 6} if result is None else result
        self.status, self.calls = status, []
        self.exception = None
    def get(self, path, params=None, **kwargs):
        self.calls.append(("GET", path, None))
        return Response(self.item)
    def put_mini(self, path, body, params=None, **kwargs):
        self.calls.append(("PUT", path, copy.deepcopy(body)))
        if self.exception:
            raise self.exception
        return Response(self.result, self.status)
    def post(self, path, body, params=None, **kwargs):
        self.calls.append(("POST", path, copy.deepcopy(body)))
        return Response({"id": 9})
    def need_login(self):
        return False


def product_probe(quantity=6, variant=False):
    company = SimpleNamespace(id=1, mercadolibre_seller_id="42", mercadolibre_client_id="synthetic", _fields={},
                              mercadolibre_post_default_code=False)
    class Environment(dict):
        context = {}
        user = SimpleNamespace(company_id=company)
    env = Environment({"meli.warning": object(), "product.product": object()})
    template = SimpleNamespace(_fields={"meli_update_stock_blocked"}, meli_update_stock_blocked=False,
        meli_pub_as_variant=variant, meli_stock_update=False, meli_stock_error=False,
        meli_pub_principal_variant=SimpleNamespace(id=1, meli_id="MLA123"))
    class Product(SimpleNamespace):
        def __iter__(self):
            yield self
        def ensure_one(self):
            return None
    product = Product(env=env, id=1, product_tmpl_id=template, company_id=SimpleNamespace(id=1),
        _fields={"meli_update_stock_blocked"}, meli_update_stock_blocked=False,
        meli_stock_update=False, meli_stock_error=False, meli_id="MLA123",
        meli_id_variation="7" if variant else False, meli_user_product_id=False,
        virtual_available=quantity, route_ids=[], meli_available_quantity=quantity,
        meli_status="active", default_code="C6", _meli_available_quantity=lambda **kw: quantity,
        _is_product_combination=lambda row: False, _combination=lambda: {"attribute_combinations": []})
    product.activations = []
    product.product_meli_status_active = lambda **kw: product.activations.append(True) or {}
    template.product_variant_ids = [product]
    # Bind only helpers that actually exist, so baseline RED is not a missing-helper error.
    tree = ast.parse((ROOT / "models/product.py").read_bytes())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_meli_stock_"):
            setattr(product, node.name, MethodType(actual("models/product.py", node.name), product))
    return product, company


class TransportTests(unittest.TestCase):
    def nosdk(self, response=None, error=None):
        session = SimpleNamespace()
        def put(*args, **kwargs):
            if error:
                raise error
            return response
        session.put = put
        client = SimpleNamespace(_session=session, _abs_url=lambda path: "https://synthetic.invalid"+path,
            _parse_response=lambda response: response.json(), response={"old": True},
            rjson={"old": True}, last_status_code=200)
        return client

    def test_nosdk_put_replaces_preceding_get_status(self):
        client = self.nosdk(Response({"error": "denied"}, 403))
        method = actual("models/meli_util.py", "put", "MeliApiNoSDK",
                        {"MeliApiNoSDK": SimpleNamespace(_benchmark_enabled=False)})
        self.assertIs(method(client, "/items/MLA123", {"available_quantity": 0}), client)
        self.assertEqual(client.last_status_code, 403, "STALE_GET_STATUS")
        self.assertEqual(client.rjson, {"error": "denied"})

    def test_nosdk_transport_failure_does_not_reuse_previous_status_or_body(self):
        client = self.nosdk(error=NetworkError("synthetic transport failure"))
        actual("models/meli_util.py", "put", "MeliApiNoSDK",
               {"MeliApiNoSDK": SimpleNamespace(_benchmark_enabled=False)})(client, "/items/MLA123", {})
        self.assertIn(client.last_status_code, (None, 0), "STALE_GET_STATUS")
        self.assertNotEqual(client.rjson, {"old": True})

    def test_sdk_mini_copies_its_own_current_http_status(self):
        class Mini:
            def __init__(self, config=None):
                self.response = self.rjson = {"error": "denied"}
                self.last_status_code = 403
            def put(self, *args, **kwargs):
                return self
        client = SimpleNamespace(response={"old": True}, rjson={"old": True}, last_status_code=200)
        result = actual("models/meli_util.py", "put_mini", "MeliApiSDK",
            {"MeliApiNoSDK": Mini, "configuration_nosdk": object()})(client, "/items/MLA123", {})
        self.assertIs(result, client)
        self.assertEqual(client.last_status_code, 403)

    def test_sdk_unexpected_exception_clears_preceding_success(self):
        def fail(**kwargs):
            raise RuntimeError("synthetic.invalid/never-log-this")
        client = SimpleNamespace(resource_put=fail, response={"old": True}, rjson={"old": True},
            last_status_code=200, api_client=SimpleNamespace(last_response=SimpleNamespace(status=200)))
        self.assertIs(actual("models/meli_util.py", "put", "MeliApiSDK")(client, "/items/MLA123", {}), client)
        self.assertIn(client.last_status_code, (None, 0))
        self.assertNotEqual(client.rjson, {"old": True})


    def test_sdk_success_captures_this_requests_http_response(self):
        client = SimpleNamespace(response={"old": True}, rjson={"old": True}, last_status_code=403,
                                 api_client=SimpleNamespace(last_response=SimpleNamespace(status=403)))
        def success(**kwargs):
            client.api_client.last_response = SimpleNamespace(status=200)
            return {"id": "MLA123", "available_quantity": 0}
        client.resource_put = success
        self.assertIs(actual("models/meli_util.py", "put", "MeliApiSDK")(client, "/items/MLA123", {}), client)
        self.assertEqual(client.last_status_code, 200)
        self.assertEqual(client.rjson, {"id": "MLA123", "available_quantity": 0})

    def test_sdk_api_error_captures_status_without_raw_exception_material(self):
        def failure(**kwargs):
            raise ApiError("synthetic.invalid/private-material")
        client = SimpleNamespace(resource_put=failure, response={"old": True}, rjson={"old": True},
                                 last_status_code=200, api_client=SimpleNamespace(last_response=None))
        actual("models/meli_util.py", "put", "MeliApiSDK")(client, "/items/MLA123", {})
        self.assertEqual(client.last_status_code, 503)
        self.assertIn("error", client.rjson)
        self.assertNotIn("private-material", str(client.rjson))


class StockOutcomeTests(unittest.TestCase):
    def invoke(self, client, quantity=6, variant=False, paused=False, blocked=False, activation_result=None):
        product, company = product_probe(quantity, variant)
        if paused:
            product.meli_status = "paused"
        product.meli_update_stock_blocked = blocked
        if activation_result is not None:
            product.product_meli_status_active = lambda **kw: activation_result
        result = actual("models/product.py", "product_post_stock")(product, meli=client, config=company)
        return result, product

    def assert_unconfirmed(self, client, **kwargs):
        result, product = self.invoke(client, **kwargs)
        self.assertIsInstance(result, dict)
        self.assertIn("error", result, "FALSE_STOCK_SUCCESS")
        self.assertFalse(product.meli_stock_update, "EARLY_SUCCESS_TIMESTAMP")
        self.assertFalse(product.product_tmpl_id.meli_stock_update)
        self.assertFalse(product.activations)
        return product

    def test_actual_activation_checks_current_raw_response(self):
        for body, status, accepted in (({}, 200, False), ({"id": "MLA999", "status": "active"}, 200, False),
                ({"id": "MLA123", "status": "active"}, 403, False),
                ({"id": "MLA123", "status": "active"}, 200, True)):
            with self.subTest(body=body, status=status):
                product, _company = product_probe()
                result = actual("models/product.py", "product_meli_status_active")(
                    product, meli=StockClient(body, status))
                self.assertEqual(result == {}, accepted)

    def test_empty_put_body_is_not_success_and_does_not_reactivate(self):
        self.assert_unconfirmed(StockClient({}), paused=True)

    def test_wrong_item_id_is_not_confirmation(self):
        self.assert_unconfirmed(StockClient({"id": "MLA999", "available_quantity": 6}))

    def test_quantity_mismatch_is_not_confirmation(self):
        self.assert_unconfirmed(StockClient({"id": "MLA123", "available_quantity": 5}))

    def test_non200_matching_body_is_not_confirmation(self):
        self.assert_unconfirmed(StockClient({"id": "MLA123", "available_quantity": 6}, 403))

    def test_exact_zero_is_a_confirmed_value_not_missing(self):
        result, product = self.invoke(StockClient({"id": "MLA123", "available_quantity": 0}), quantity=0)
        self.assertEqual(result, {})
        self.assertTrue(product.meli_stock_update)

    def test_matching_legacy_response_confirms_only_observed_state(self):
        result, product = self.invoke(StockClient())
        self.assertEqual(result, {})
        self.assertTrue(product.meli_stock_update)

    def test_blocked_product_does_not_stamp_or_send(self):
        client = StockClient()
        self.assert_unconfirmed(client, blocked=True)
        self.assertEqual(client.calls, [])

    def test_missing_seller_classification_refuses_before_put(self):
        client = StockClient()
        del client._meli_stock_seller_snapshot
        self.assert_unconfirmed(client)
        self.assertFalse(any(call[0] == "PUT" for call in client.calls))

    def test_changed_client_or_config_cannot_reuse_previous_classification(self):
        for changed in ("client", "config"):
            with self.subTest(changed=changed):
                client = StockClient()
                product, company = product_probe()
                if changed == "client":
                    client.client_id = "other-synthetic-app"
                else:
                    company.mercadolibre_client_id = "other-synthetic-app"
                result = actual("models/product.py", "product_post_stock")(product, meli=client, config=company)
                self.assertIn("error", result, "STALE_CLIENT_CONFIGURATION_PROOF")
                self.assertFalse(product.meli_stock_update)
                self.assertEqual(client.calls, [])

    def test_warehouse_management_alone_refuses_before_put(self):
        client = StockClient()
        client._meli_stock_seller_snapshot["body"]["tags"] = ["warehouse_management"]
        self.assert_unconfirmed(client)
        self.assertFalse(any(call[0] == "PUT" for call in client.calls))

    def test_user_product_item_refuses_before_put(self):
        client = StockClient()
        client.item["user_product_id"] = "MLAU123"
        self.assert_unconfirmed(client)
        self.assertFalse(any(call[0] == "PUT" for call in client.calls))

    def test_unsupported_direct_variation_never_rebinds_or_creates(self):
        client = StockClient()
        client.item["variations"] = [{"id": 8, "price": 100, "available_quantity": 5}]
        product = self.assert_unconfirmed(client, variant=True)
        self.assertEqual(product.meli_id_variation, "7")
        self.assertFalse(any(call[0] in ("PUT", "POST") for call in client.calls))

    def test_confirmed_stock_then_activation_error_is_partial_not_success(self):
        result, product = self.invoke(StockClient(), paused=True,
                                      activation_result={"error": "activation_failed"})
        self.assertIn("error", result)
        self.assertTrue(result.get("stock_confirmed"))
        self.assertTrue(product.meli_stock_update)

    def test_legacy_notification_with_errors_is_failed(self):
        record = SimpleNamespace(_assert_legacy_notifications=lambda: None)
        actual("models/notification.py", "stop_internal_notification")(record, errors="stock_unconfirmed")
        self.assertEqual(record.state, "FAILED")


class LifecycleAndCallerTests(unittest.TestCase):
    def test_successful_probe_cannot_survive_failed_incomplete_or_other_seller_probe(self):
        company = SimpleNamespace(id=1, mercadolibre_seller_id="42", mercadolibre_client_id="synthetic")
        client = SimpleNamespace(seller_id="42", client_id="synthetic", access_token=None, last_status_code=200)
        probe = actual("models/meli_util.py", "_meli_identity_probe")
        for status, body in ((403, {}), (200, {}), (200, {"id": 43, "tags": []}),
                             (200, {"id": 42, "tags": [], "error": "invalid"})):
            client.get = lambda *a, **k: Response({"id": 42, "tags": []})
            probe(object(), client, company)
            self.assertIsInstance(client._meli_stock_seller_snapshot, dict)
            client.get = lambda *a, **k: Response(body, status)
            probe(object(), client, company)
            self.assertIsNone(client._meli_stock_seller_snapshot)
        client.get = lambda *a, **k: Response({"id": 42, "tags": []})
        probe(object(), client, company)
        def failed(*args, **kwargs):
            raise NetworkError("synthetic probe failure")
        client.get = failed
        with self.assertRaises(NetworkError):
            probe(object(), client, company)
        self.assertIsNone(client._meli_stock_seller_snapshot)


    def test_swallowed_sdk_probe_failure_cannot_reuse_previous_seller_body(self):
        client = SimpleNamespace(seller_id="42", client_id="synthetic", access_token="synthetic", response=None,
                                 rjson=None, last_status_code=None,
                                 api_client=SimpleNamespace(last_response=None))
        def success(**kwargs):
            client.api_client.last_response = SimpleNamespace(status=200)
            return {"id": 42, "tags": []}
        client.resource_get = success
        client.json = lambda: client.rjson
        client.get = MethodType(actual("models/meli_util.py", "get", "MeliApiSDK"), client)
        company = SimpleNamespace(id=1, mercadolibre_seller_id="42", mercadolibre_client_id="synthetic")
        probe = actual("models/meli_util.py", "_meli_identity_probe")
        probe(object(), client, company)
        self.assertIsInstance(client._meli_stock_seller_snapshot, dict)
        def failure(**kwargs):
            raise RuntimeError("synthetic failed probe")
        client.resource_get = failure
        probe(object(), client, company)
        self.assertIsNone(client._meli_stock_seller_snapshot, "STALE_SELLER_PROOF_AFTER_SWALLOWED_ERROR")


    def template_outcome(self, outcomes):
        class Template(SimpleNamespace):
            def __iter__(self):
                yield self
        variants = []
        for outcome in outcomes:
            row = SimpleNamespace(product_post_stock=lambda result=outcome, **kw: result)
            row.with_context = lambda *a, row=row, **kw: row
            variants.append(row)
        template = Template(env=SimpleNamespace(context={}), product_variant_ids=variants,
                            meli_stock_update=False, meli_stock_error=False)
        result = actual("models/product.py", "product_template_post_stock")(template, meli=StockClient())
        self.assertEqual(result, outcomes, "Preserve the existing per-variant result list")
        return template

    def test_template_timestamp_requires_all_variant_confirmations(self):
        self.assertTrue(self.template_outcome([{}, {}]).meli_stock_update)

    def test_template_partial_failure_keeps_aggregate_timestamp_unchanged(self):
        template = self.template_outcome([{}, {"error": "stock_write_unconfirmed"}])
        self.assertFalse(template.meli_stock_update)
        self.assertEqual(template.meli_stock_error, "stock_batch_incomplete")

    def wizard(self, template=False, two=False):
        client = StockClient()
        failed = {"error": "stock_write_unconfirmed"}
        row = SimpleNamespace(meli_pub=True, product_post_stock=lambda **kw: failed,
            product_post_price=lambda **kw: {}, product_template_post_stock=lambda **kw: [failed, {}],
            product_template_post_price=lambda **kw: {})
        row.with_context = lambda *a, **kw: row
        good = SimpleNamespace(meli_pub=True, product_post_stock=lambda **kw: {},
                               product_post_price=lambda **kw: {})
        class Environment(dict):
            context = {"active_ids": [1, 2] if two else [1]}
            user = SimpleNamespace(company_id=SimpleNamespace(id=1))
        env = Environment({"meli.warning": object(),
            "meli.util": SimpleNamespace(get_new_instance=lambda company: client),
            "product.product": SimpleNamespace(browse=lambda oid: row if oid == 1 else good),
            "product.template": SimpleNamespace(browse=lambda oid: row)})
        wizard = SimpleNamespace(env=env, force_meli_pub=False, force_meli_active=False,
            force_meli_variant=False, post_stock=True, post_price=not two,
            post_title=False, action_pause=False)
        return actual("models/product_post.py",
            "product_template_post" if template else "product_post",
            "product_template_post" if template else "product_post")(wizard)

    def test_product_wizard_later_price_success_cannot_mask_stock_error(self):
        self.assertIn("error", self.wizard())

    def test_template_wizard_nested_stock_errors_cannot_be_masked(self):
        self.assertIn("error", self.wizard(template=True))

    def test_next_product_success_cannot_mask_previous_stock_error(self):
        self.assertIn("error", self.wizard(two=True))

    def batch(self, name):
        errors, diagnostics = [], []
        product = SimpleNamespace(meli_id="MLA123", default_code="C6", meli_available_quantity=6,
            meli_shipping_logistic_type="drop_off",
            product_post_stock=lambda **kw: {"error": "stock_write_unconfirmed"})
        notification = SimpleNamespace(stop_internal_notification=lambda **kw: errors.append(kw["errors"]))
        company = SimpleNamespace(id=1, mercadolibre_cron_post_update_stock=True,
            mercadolibre_client_id="synthetic", mercadolibre_seller_id="42")
        class Environment(dict):
            user = SimpleNamespace(company_id=company)
            cr = SimpleNamespace(execute=lambda *a: None, fetchall=lambda: [(1,)])
        env = Environment({"product.product": SimpleNamespace(browse=lambda ids: [product]),
            "mercadolibre.notification": SimpleNamespace(start_internal_notification=lambda values: notification)})
        probe = SimpleNamespace(env=env, meli_stock_diagnostic=lambda **kw: diagnostics.append(True))
        scope = {"threading": SimpleNamespace(current_thread=lambda: SimpleNamespace(testing=True))}
        result = actual("models/company.py", name, extra=scope)(probe, meli=StockClient())
        self.assertIn("error", result)
        self.assertTrue(errors[0])
        self.assertEqual(diagnostics, [], "An uncertain write must not trigger a blind diagnostic retry")

    def test_stock_batch_reports_failure_not_empty_success(self):
        self.batch("meli_update_remote_stock")

    def test_realtime_stock_batch_reports_failure(self):
        self.batch("meli_update_remote_stock_rt")

    def diagnostic(self, status):
        company = SimpleNamespace(id=1, name="C6", mercadolibre_cron_post_update_stock=True)
        product = SimpleNamespace(meli_update_stock_blocked=False,
            product_tmpl_id=SimpleNamespace(meli_update_stock_blocked=False),
            product_post_stock=lambda **kw: {"error": "stock_write_unconfirmed"})
        product._meli_stock_payload = MethodType(actual("models/product.py", "_meli_stock_payload"), product)
        class Environment(dict):
            user = SimpleNamespace(company_id=company)
            cr = SimpleNamespace(execute=lambda *a: None,
                fetchall=lambda: [(1, "C6", "MLA123", 0, 6, "drop_off")])
        env = Environment({"product.product": SimpleNamespace(browse=lambda oid: product)})
        probe = SimpleNamespace(env=env, _name="test.probe",
                                _meli_diag_persist_ml_status=lambda *a: None)
        client = StockClient()
        client.item.update(status=status, available_quantity=0)
        scope = {"threading": SimpleNamespace(current_thread=lambda: SimpleNamespace(testing=True))}
        result = actual("models/company.py", "meli_stock_diagnostic", extra=scope)(probe, meli=client)
        self.assertIn("error", result)
        self.assertEqual(result["actions_taken"], 0)

    def test_diagnostic_does_not_claim_reactivation_after_stock_refusal(self):
        self.diagnostic("paused")

    def test_diagnostic_does_not_claim_push_after_stock_refusal(self):
        self.diagnostic("active")


if __name__ == "__main__":
    unittest.main()
