"""Source-bound intake contracts, not real HTTP/ORM/durability acceptance."""

import ast
import copy
import importlib.util
import io
import json
from contextlib import contextmanager
from pathlib import Path
import socket
import sys
from types import MethodType, ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"


def load_helper():
    package = ModuleType("inbox_contract_models")
    package.__path__ = [str(MODELS)]
    sys.modules[package.__name__] = package
    path = MODELS / "meli_webhook_inbox.py"
    spec = importlib.util.spec_from_file_location(package.__name__ + ".inbox", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def methods(path, class_name, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    functions = [copy.deepcopy(n) for n in cls.body if isinstance(n, ast.FunctionDef)]
    for function in functions:
        function.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return {n.name: namespace[n.name] for n in functions}


class IntakeContracts(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket, "socket", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.h = load_helper()
        self.payload = {"_id": "opaque:/delivery", "application_id": 123, "user_id": 456,
                        "topic": "orders_v2", "resource": "/orders/789"}

    def parse(self, **changes):
        return self.h.parse_webhook_body(json.dumps(dict(self.payload, **changes)).encode())

    def rejects(self, status, **changes):
        with self.assertRaises(self.h.WebhookIntakeError) as error:
            self.parse(**changes)
        self.assertEqual(error.exception.status, status)

    def test_selected_fields_only(self):
        result = self.parse(headers={"Authorization": "synthetic"}, email="private@example.invalid",
                            company_id=99, context={"allowed_company_ids": [99]})
        self.assertEqual(set(result), set(self.payload))
        self.assertEqual(result["application_id"], "123")
        self.assertEqual(result["user_id"], "456")

    def test_canonical_numeric_ids(self):
        self.assertEqual(self.parse(application_id="123", user_id="456")["application_id"], "123")
        for value in (True, False, 1.0, 0, -1, "01", " 1", "+1", "1e2", "١", 2**63, None):
            with self.subTest(value=value):
                self.rejects(400, application_id=value)
                self.rejects(400, user_id=value)

    def test_topic_resource_pair_grammar(self):
        for topic, resource in (("orders_v2", "/orders/1"), ("items", "/items/MLA123"),
                                ("questions", "/questions/1"), ("shipments", "/shipments/1")):
            self.assertEqual(self.parse(topic=topic, resource=resource)["resource"], resource)
        for resource in ("https://example.invalid/orders/1", "//example.invalid/orders/1",
                         "/orders/1?x=1", "/orders/1#x", "/orders/%31", "/orders/1/2",
                         "/orders/../1", "/orders/0", "/orders/01", " /orders/1", "/orders/1\n"):
            with self.subTest(resource=resource):
                self.rejects(400, resource=resource)
        self.rejects(400, topic="items", resource="/items/MLC123")
        self.rejects(400, topic="items", resource="/orders/1")

    def test_unsupported_and_unproved_payment_topics_reject(self):
        for topic in ("unknown", "order", "created_orders", "orders", "payments"):
            self.rejects(422, topic=topic, resource="/collections/1")

    def test_json_object_duplicates_and_invalid_encoding(self):
        for raw in (b"[]", b"null", b"{}", b'{"application_id":123,"application_id":123}',
                    b'{"nested":{"x":1,"x":2}}', b'{"application_id":NaN}', b"\xff", b"{"):
            with self.subTest(raw=raw):
                with self.assertRaises(self.h.WebhookIntakeError):
                    self.h.parse_webhook_body(raw)

    def test_exact_body_limit_and_max_id_are_accepted(self):
        raw = json.dumps(dict(self.payload, application_id=2**63 - 1)).encode()
        self.assertEqual(self.h.parse_webhook_body(raw.ljust(16384, b" "))["application_id"], str(2**63 - 1))

    def test_body_and_provider_id_bounds(self):
        with self.assertRaises(self.h.WebhookIntakeError) as error:
            self.h.parse_webhook_body(b" " * 16385)
        self.assertEqual(error.exception.status, 413)
        for value in ("", 12, True, "x" * 257, "bad\nvalue", "\x00", "é" * 129):
            self.rejects(400, _id=value)

    def test_advisory_timestamps_and_attempts(self):
        value = self.parse(attempts=2, sent="2026-10-13T12:00:00.123Z",
                           received="2026-10-13T13:00:00+01:00")
        self.assertEqual(value["attempts"], 2)
        self.assertEqual(value["sent"], "2026-10-13T12:00:00.123000Z")
        self.assertEqual(value["received"], "2026-10-13T12:00:00Z")
        for value in (True, -1, 1.2, "2", 1000001):
            self.rejects(400, attempts=value)
        for value in ("not-a-date", "2026-10-13T12:00:00", "x" * 41):
            self.rejects(400, sent=value)

    def test_receipt_identity_namespaces_and_no_id_arrivals(self):
        value = self.parse()
        self.assertEqual(self.h.webhook_receipt_id(value), "ml:123:456:b3BhcXVlOi9kZWxpdmVyeQ")
        del value["_id"]
        ids = {self.h.webhook_receipt_id(value) for _ in range(20)}
        self.assertEqual(len(ids), 20)
        self.assertTrue(all(x.startswith("receipt:") for x in ids))

    def test_resource_helper_query_compatibility_is_preserved(self):
        self.assertEqual(self.h.safe_meli_resource_path("/orders/1?x=1"), "/orders/1?x=1")


class FakeUniqueViolation(Exception):
    def __init__(self, name):
        self.diag = SimpleNamespace(constraint_name=name)


class FakeNotification:
    def __init__(self, functions, rows=None):
        for name, function in functions.items():
            setattr(self, name, function.__get__(self))
        self.rows = rows if rows is not None else []
        self.env = SimpleNamespace(cr=self)
        self._table = "mercadolibre_notification"
        self._unique_notification_id = SimpleNamespace(full_name=lambda model: "mercadolibre_notification_unique_notification_id")
        self.binding = {"company_id": 7, "actor_id": 12, "application_id": "123", "user_id": "456"}
        self._webhook_binding = Mock(return_value=self.binding)
        self.failures = []
        self.creates = []
        self.searches = []

    def with_context(self, *args, **kwargs):
        return self

    def __iter__(self):
        return iter(self.rows)

    def sudo(self):
        return self

    @contextmanager
    def savepoint(self):
        yield

    def search(self, domain, **kwargs):
        self.searches.append(domain)
        def matches(row):
            tokens = iter(domain)
            def condition():
                node = next(tokens)
                if node == "!":
                    return not condition()
                if node in ("&", "|"):
                    left, right = condition(), condition()
                    return left and right if node == "&" else left or right
                field, operator, expected = node
                actual = getattr(row, field, False)
                actual = actual.id if hasattr(actual, "id") else actual
                if operator == "=":
                    return actual == expected
                if operator == "in":
                    return actual in expected
                if operator == "=like":
                    return actual.startswith(expected.removesuffix("%"))
                raise AssertionError("unsupported fake domain operator")
            results = []
            while True:
                try:
                    results.append(condition())
                except StopIteration:
                    return all(results)
        rows = [row for row in self.rows if matches(row)]
        return rows[:kwargs["limit"]] if kwargs.get("limit") else rows

    def create(self, values):
        if self.failures:
            raise self.failures.pop(0)
        self.creates.append(copy.deepcopy(values))
        row = SimpleNamespace(**values)
        row.company_id = SimpleNamespace(id=values["company_id"])
        row.seller_id = SimpleNamespace(id=values["seller_id"])
        row.flush_recordset = Mock()
        self.rows.append(row)
        return row


class PersistenceContracts(unittest.TestCase):
    parse = IntakeContracts.parse

    def setUp(self):
        IntakeContracts.setUp(self)
        namespace = dict(vars(self.h), UniqueViolation=FakeUniqueViolation, AccessError=PermissionError,
                         ValidationError=ValueError,
                         fields=SimpleNamespace(Datetime=SimpleNamespace(to_datetime=lambda x: x)))
        self.functions = methods(MODELS / "notification.py", "MercadolibreNotification", namespace)
        self.model = FakeNotification(self.functions)

    def receive(self, **changes):
        return self.model._receive_webhook(json.dumps(dict(self.payload, **changes)).encode())

    def test_canonical_insert_explicit_binding_without_dispatch(self):
        self.receive()
        values = self.model.creates[0]
        self.assertEqual((values["company_id"], values["seller_id"], values["state"]), (7, 12, "RECEIVED"))
        self.assertEqual(json.loads(values["parameters"]), dict(self.parse(), _inbox_receipt=values["notification_id"]))
        self.assertEqual(values["user_id"], "456")

    def test_foreign_identity_and_missing_binding_fail_closed(self):
        self.model._webhook_binding.side_effect = self.h.WebhookIntakeError(503, "binding_unavailable")
        with self.assertRaises(self.h.WebhookIntakeError):
            self.receive()
        self.assertFalse(self.model.creates)
        self.model._webhook_binding.side_effect = None
        for key in ("application_id", "user_id"):
            with self.assertRaises(self.h.WebhookIntakeError) as error:
                self.receive(**{key: 999})
            self.assertEqual(error.exception.status, 403)
        self.assertFalse(self.model.creates)

    def test_connector_without_bridge_fails_closed(self):
        with self.assertRaises(self.h.WebhookIntakeError) as error:
            self.functions["_webhook_binding"](self.model)
        self.assertEqual(error.exception.status, 503)

    def test_duplicate_every_state_is_read_only(self):
        self.receive()
        for state in ("RECEIVED", "PROCESSING", "SUCCESS", "FAILED"):
            self.model.rows[0].state = state
            before = vars(self.model.rows[0]).copy()
            self.receive(attempts=9)
            self.assertEqual(vars(self.model.rows[0]), before)
        self.assertEqual(len(self.model.creates), 1)

    def test_legacy_matching_raw_id_deduplicates_without_rewrite(self):
        self.receive()
        self.model.rows[0].notification_id = self.payload["_id"]
        self.receive()
        self.assertEqual(len(self.model.creates), 1)

    def test_conflict_binding_preserves_original_and_quarantines_arrival(self):
        self.receive()
        for field, foreign in (("resource", "/orders/999"), ("topic", "items"),
                               ("application_id", "999"), ("user_id", "999"),
                               ("company_id", SimpleNamespace(id=99)), ("seller_id", SimpleNamespace(id=99))):
            with self.subTest(field=field):
                row = self.model.rows[0]
                original = getattr(row, field)
                setattr(row, field, foreign)
                before = vars(row).copy()
                self.receive()
                self.assertEqual(vars(row), before)
                quarantine = self.model.creates[-1]
                self.assertEqual(quarantine["state"], "FAILED")
                self.assertTrue(quarantine["notification_id"].startswith("receipt:"))
                self.assertEqual(quarantine["processing_errors"], "provider_id_conflict")
                setattr(row, field, original)

    def test_specific_unique_violation_invisible_winner_is_not_ack(self):
        self.model.failures = [FakeUniqueViolation(self.model._unique_notification_id.full_name(self.model))]
        with self.assertRaises(self.h.WebhookIntakeError) as error:
            self.receive()
        self.assertEqual(error.exception.status, 503)

    def test_specific_unique_violation_visible_winner_is_read_only(self):
        self.receive()
        winner = self.model.rows.pop()
        original_create = self.model.create
        def concurrent_create(values):
            self.model.rows.append(winner)
            raise FakeUniqueViolation(self.model._unique_notification_id.full_name(self.model))
        self.model.create = concurrent_create
        before = vars(winner).copy()
        self.receive()
        self.assertEqual(vars(winner), before)
        self.assertEqual(len(self.model.creates), 1)
        self.model.create = original_create

    def test_mixed_matching_and_conflicting_legacy_rows_quarantine(self):
        self.receive()
        raw = copy.copy(self.model.rows[0])
        raw.notification_id = self.payload["_id"]
        raw.company_id = SimpleNamespace(id=99)
        self.model.rows.append(raw)
        self.receive()
        self.assertEqual(self.model.creates[-1]["state"], "FAILED")

    def test_no_id_receipts_are_independent_and_repeated_uuid_collision_fails(self):
        raw = dict(self.payload)
        del raw["_id"]
        self.model._receive_webhook(json.dumps(raw).encode())
        self.model._receive_webhook(json.dumps(raw).encode())
        self.assertEqual(len(self.model.creates), 2)
        self.assertNotEqual(self.model.creates[0]["notification_id"], self.model.creates[1]["notification_id"])
        self.model.failures = [FakeUniqueViolation(self.model._unique_notification_id.full_name(self.model))] * 3
        with self.assertRaises(self.h.WebhookIntakeError) as error:
            self.model._receive_webhook(json.dumps(raw).encode())
        self.assertEqual(error.exception.status, 503)

    def test_flush_failure_is_not_ack(self):
        original_create = self.model.create
        def failing_create(values):
            row = original_create(values)
            row.flush_recordset.side_effect = RuntimeError("flush failed")
            return row
        self.model.create = failing_create
        with self.assertRaises(RuntimeError):
            self.receive()

    def test_unrelated_unique_or_database_error_propagates(self):
        for failure in (FakeUniqueViolation("unrelated_constraint"), RuntimeError("db failed")):
            self.model.failures = [failure]
            with self.assertRaises(type(failure)):
                self.receive()

    def test_local_uuid_collision_retries_not_dropped(self):
        raw = dict(self.payload)
        del raw["_id"]
        self.model.failures = [FakeUniqueViolation(self.model._unique_notification_id.full_name(self.model))]
        self.model._receive_webhook(json.dumps(raw).encode())
        self.assertEqual(len(self.model.creates), 1)
        values = self.model.creates[0]
        self.assertEqual(json.loads(values["parameters"])["_inbox_receipt"], values["notification_id"])

    def test_new_receipts_all_legacy_entrypoints_stop_before_client(self):
        for name in ("process_notification", "_process_notification_order", "_process_notification_question",
                     "_process_notification_item", "stop_internal_notification", "fetch_lasts", "process_notifications"):
            for identity in ("ml:123:456:eA", "receipt:synthetic"):
                target = FakeNotification(self.functions)
                target.rows = [SimpleNamespace(notification_id=identity)]
                with self.assertRaises(PermissionError):
                    getattr(target, name)()

    def test_legacy_company_dispatch_of_namespaced_id_stops_before_client(self):
        namespace = {}
        functions = methods(MODELS / "company.py", "res_company", namespace)
        company = SimpleNamespace(mercadolibre_process_notifications=True,
                                  env={"mercadolibre.notification": self.model})
        for identity in ("ml:123:456:eA", "receipt:synthetic"):
            with self.assertRaises(PermissionError):
                functions["meli_notifications"](company, dict(self.payload, _id=identity))

    def test_actual_persisted_parameters_replay_is_denied_with_or_without_marker(self):
        for missing_id in (False, True):
            for strip_marker in (False, True):
                for company_dispatch in (False, True):
                    with self.subTest(missing_id=missing_id, strip_marker=strip_marker, company_dispatch=company_dispatch):
                        source = FakeNotification(self.functions)
                        payload = dict(self.payload)
                        if missing_id:
                            payload.pop("_id")
                        receipt = source._receive_webhook(json.dumps(payload).encode())
                        receipt.state = "PROCESSING"
                        before = vars(receipt).copy()
                        data = json.loads(receipt.parameters)
                        if strip_marker:
                            data.pop("_inbox_receipt", None)
                        class EmptyCollection(FakeNotification):
                            def __iter__(self):
                                return iter(())
                        collection = EmptyCollection(self.functions, source.rows)
                        company = SimpleNamespace(id=7, name="Synthetic company", mercadolibre_seller_user=False,
                                                  mercadolibre_process_notifications=True, mercadolibre_client_id="123")
                        utility = SimpleNamespace(get_new_instance=Mock(side_effect=RuntimeError("client sentinel")))
                        class Env(dict):
                            user = SimpleNamespace(name="Synthetic actor", company_id=company)
                        collection.env = Env({"meli.util": utility})
                        company.env = {"mercadolibre.notification": collection}
                        self.functions["fetch_lasts"].__globals__["_logger"] = Mock()
                        company_method = methods(MODELS / "company.py", "res_company", {})["meli_notifications"]
                        try:
                            company_method(company, data) if company_dispatch else collection.fetch_lasts(data, company)
                        except (PermissionError, RuntimeError):
                            pass
                        self.assertEqual(utility.get_new_instance.call_count, 0)
                        self.assertEqual(vars(receipt), before)

    def test_caller_provenance_can_only_deny_never_choose_receipt_identity(self):
        receipt = self.receive(_inbox_receipt="caller-controlled")
        stored = json.loads(receipt.parameters)
        self.assertEqual(stored["_inbox_receipt"], receipt.notification_id)
        self.assertNotEqual(stored["_inbox_receipt"], "caller-controlled")
        with self.assertRaises(PermissionError):
            FakeNotification(self.functions).fetch_lasts(dict(self.payload, _inbox_receipt="caller-controlled"),
                                                       SimpleNamespace(id=7))

    def test_genuine_legacy_data_without_new_receipt_keeps_client_entry(self):
        target = FakeNotification(self.functions)
        target.rows = [SimpleNamespace(notification_id="historical", application_id="123", user_id="456",
                                       topic="orders_v2", resource="/orders/789", company_id=SimpleNamespace(id=7))]
        target.__class__ = type("EmptyCollection", (FakeNotification,), {"__iter__": lambda self: iter(())})
        company = SimpleNamespace(id=7, name="Synthetic legacy", mercadolibre_seller_user=False,
                                  mercadolibre_client_id="123")
        utility = SimpleNamespace(get_new_instance=Mock(side_effect=RuntimeError("client sentinel")))
        class Env(dict):
            user = SimpleNamespace(name="Synthetic actor", company_id=company)
        target.env = Env({"meli.util": utility})
        self.functions["fetch_lasts"].__globals__["_logger"] = Mock()
        with self.assertRaises(RuntimeError):
            target.fetch_lasts(self.payload, company)
        self.assertEqual(utility.get_new_instance.call_count, 1)

    def test_batch_excludes_both_new_namespaces(self):
        target = FakeNotification(self.functions)
        target.search = Mock(return_value=[])
        # This runs the original method; source-bound logging is inert.
        self.functions["process_notifications"].__globals__["_logger"] = Mock()
        target.process_notifications()
        domain = target.search.call_args.args[0]
        self.assertIn(("notification_id", "=like", "ml:%"), domain)
        self.assertIn(("notification_id", "=like", "receipt:%"), domain)
        self.assertEqual(domain.count("!"), 2)


class ControllerContracts(unittest.TestCase):
    def setUp(self):
        self.h = load_helper()
        class Response:
            def __init__(self, body="", status=200, **kwargs):
                self.status_code, self.body = status, body
        self.model = Mock()
        # Odoo19 HTTPRequest exposes get_data/max_content_length, not .stream
        # or raw wsgi.input in its public environ. Do not model Werkzeug here.
        self.incoming = SimpleNamespace(mimetype="application/json", content_length=2,
                                        environ={"CONTENT_LENGTH": "2"}, max_content_length=64 * 1024 * 1024,
                                        get_data=Mock(return_value=b"{}"))
        self.request = SimpleNamespace(env={"mercadolibre.notification": self.model}, httprequest=self.incoming)
        self.disconnected = type("SyntheticClientDisconnected", (Exception,), {})
        self.too_large = type("SyntheticRequestEntityTooLarge", (Exception,), {})
        self.ns = dict(request=self.request, Response=Response, WebhookIntakeError=self.h.WebhookIntakeError,
                       WEBHOOK_MAX_BYTES=16384, ClientDisconnected=self.disconnected,
                       RequestEntityTooLarge=self.too_large)
        self.functions = methods(ROOT / "controllers/main.py", "MercadoLibre", self.ns)

    def test_http_route_and_empty_nonstreaming_response(self):
        tree = ast.parse((ROOT / "controllers/main.py").read_text(encoding="utf-8"))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "meli_notify")
        route = method.decorator_list[0]
        def route_value(value):
            if isinstance(value, ast.Name) and value.id == "WEBHOOK_MAX_BYTES":
                return self.h.WEBHOOK_MAX_BYTES
            if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
                return route_value(value.left) + ast.literal_eval(value.right)
            return ast.literal_eval(value)
        options = {x.arg: route_value(x.value) for x in route.keywords}
        self.assertEqual(options, dict(type="http", auth="public", methods=["POST"], csrf=False,
                                      readonly=False, save_session=False, max_content_length=16385))
        self.assertEqual(ast.literal_eval(route.args[0]), ["/meli_notify", "/odoo/meli_notify"])
        response = self.functions["meli_notify"](None, company_id=99)
        self.assertEqual((response.status_code, response.body), (200, ""))
        self.model._receive_webhook.assert_called_once_with(b"{}")
        self.incoming.get_data.assert_called_once_with(cache=False, as_text=False, parse_form_data=False)
        self.assertEqual(self.incoming.max_content_length, 3)

    def test_content_type_length_errors_and_unknown_db_failures(self):
        self.request.httprequest.mimetype = "text/plain"
        self.assertEqual(self.functions["meli_notify"](None).status_code, 415)
        self.model._receive_webhook.assert_not_called()
        self.request.httprequest.mimetype = "application/json"
        self.request.httprequest.content_length = 16385
        self.assertEqual(self.functions["meli_notify"](None).status_code, 413)
        self.request.httprequest.content_length = None
        self.assertEqual(self.functions["meli_notify"](None).status_code, 411)
        self.incoming.get_data.assert_not_called()
        self.request.httprequest.content_length = 2
        self.model._receive_webhook.side_effect = self.h.WebhookIntakeError(403, "foreign_identity")
        self.assertEqual(self.functions["meli_notify"](None).status_code, 403)
        self.model._receive_webhook.side_effect = RuntimeError("synthetic sensitive db error")
        with self.assertRaises(RuntimeError):
            self.functions["meli_notify"](None)

    def test_odoo_facade_known_length_read_is_capped_before_supported_accessor(self):
        def read(**kwargs):
            self.assertEqual(self.incoming.max_content_length, 3)
            self.assertFalse(hasattr(self.incoming, "stream"))
            self.assertNotIn("wsgi.input", self.incoming.environ)
            return b"{}"
        self.incoming.get_data.side_effect = read
        self.assertEqual(self.functions["meli_notify"](None).status_code, 200)

    def test_unknown_chunked_and_invalid_length_never_read(self):
        for length, transfer in ((None, None), (None, "chunked"), (2, "chunked"), (-1, None), (True, None)):
            with self.subTest(length=length, transfer=transfer):
                self.incoming.content_length = length
                self.incoming.environ = {} if transfer is None else {"HTTP_TRANSFER_ENCODING": transfer}
                self.assertEqual(self.functions["meli_notify"](None).status_code, 411)
        self.incoming.get_data.assert_not_called()
        self.model._receive_webhook.assert_not_called()

    def test_disconnect_and_capped_stream_failures_never_ack(self):
        for failure, status in ((self.disconnected(), 400), (OSError("synthetic disconnect"), 400),
                                (self.too_large(), 413)):
            self.incoming.get_data.side_effect = failure
            self.assertEqual(self.functions["meli_notify"](None).status_code, status)
        self.model._receive_webhook.assert_not_called()

    def test_public_reader_rejects_declared_length_mismatch_and_excess_probe(self):
        for raw, status in ((b"{", 400), (b"{}x", 400), (b"x" * 16385, 413)):
            self.incoming.get_data.side_effect = None
            self.incoming.get_data.return_value = raw
            self.assertEqual(self.functions["meli_notify"](None).status_code, status)
        self.model._receive_webhook.assert_not_called()


class LegacyOrderResultContracts(unittest.TestCase):
    """Actual legacy processor, synthetic producer and savepoint boundary only."""

    def setUp(self):
        self.network = patch.object(socket, "socket", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        helper = load_helper()
        namespace = dict(vars(helper), ValidationError=ValueError, _logger=Mock(),
                         ml_datetime=lambda value: value)
        self.functions = methods(MODELS / "notification.py", "MercadolibreNotification", namespace)
        self.store = {"caller": "preserved", "effects": []}
        self.row = self.Row()
        self.cr = self.Cursor(self.store, self.row)
        self.result = {}
        self.failure = None
        self.client = SimpleNamespace(access_token="synthetic", get=Mock(return_value=SimpleNamespace(
            json=lambda: {"id": 789})))
        class Orders(SimpleNamespace):
            def __len__(self):
                return 1
        orders = Orders(id=12)
        orders.search = lambda domain, limit: orders
        orders.with_context = lambda **context: orders
        orders.orders_update_order_json = self.produce
        class Environment(dict):
            pass
        env = Environment({"meli.util": SimpleNamespace(get_new_instance=lambda company: self.client),
                           "mercadolibre.orders": orders})
        env.user = SimpleNamespace(company_id=SimpleNamespace(id=1))
        env.cr = self.cr
        class Records(SimpleNamespace):
            def __iter__(records):
                return iter(records.rows)
        self.records = Records(env=env, rows=[self.row])
        self.records._assert_legacy_notifications = MethodType(
            self.functions["_assert_legacy_notifications"], self.records)

    class Row:
        def __init__(self):
            object.__setattr__(self, "values", dict(notification_id="legacy-synthetic", resource="/orders/789",
                state="RECEIVED", processing_started=False, processing_ended=False,
                processing_errors=False, last_json_response=False))
            object.__setattr__(self, "fault", None)

        def __getattr__(self, name):
            return self.values[name]

        def __setattr__(self, name, value):
            if name == "fault":
                object.__setattr__(self, name, value)
                return
            if self.fault == name and (name != "state" or value == "SUCCESS"):
                object.__setattr__(self, "fault", None)
                raise RuntimeError("synthetic outcome write refused")
            self.values[name] = value

    class Cursor:
        def __init__(self, store, row):
            self.store, self.row = store, row
            self.global_boundaries = []

        @contextmanager
        def savepoint(self):
            before, fields = copy.deepcopy(self.store), copy.deepcopy(self.row.values)
            try:
                yield
            except BaseException:
                self.store.clear()
                self.store.update(before)
                self.row.values.clear()
                self.row.values.update(fields)
                raise

        def commit(self):
            self.global_boundaries.append("commit")
            raise AssertionError("internal commit forbidden")

        def rollback(self):
            self.global_boundaries.append("rollback")
            raise AssertionError("global rollback forbidden")

    def produce(self, data):
        self.assertEqual(data, {"id": 12, "order_json": {"id": 789}})
        self.store["effects"].append("producer")
        if self.failure:
            raise self.failure
        return self.result

    def invoke(self):
        self.functions["_process_notification_order"](self.records)
        self.assertEqual(self.client.get.call_args.args[0], "/orders/789")
        self.client.get.assert_called_once()
        self.assertEqual(self.store["caller"], "preserved")
        self.assertEqual(self.cr.global_boundaries, [])

    def assert_unconfirmed(self, result):
        self.result = result
        self.invoke()
        self.assertEqual(self.row.state, "FAILED")
        self.assertTrue(self.row.processing_errors)
        self.assertTrue(self.row.processing_ended)
        self.assertEqual(self.store["effects"], [], "Producer effects must roll back, not caller work")

    def test_false_result_rolls_back_producer_only(self):
        self.assert_unconfirmed(False)

    def test_none_result_rolls_back_producer_only(self):
        self.assert_unconfirmed(None)

    def test_non_dictionary_result_is_not_verified_success(self):
        for result in (True, 0, [], ["unexpected"], "unexpected"):
            with self.subTest(result=result):
                self.setUp_result_iteration()
                self.assert_unconfirmed(result)

    def setUp_result_iteration(self):
        self.store["effects"].clear()
        self.client.get.reset_mock()

    def test_empty_dictionary_retains_legacy_success(self):
        self.invoke()
        self.assertEqual(self.row.state, "SUCCESS")
        self.assertEqual(self.store["effects"], ["producer"])
        self.assertTrue(self.row.processing_ended)

    def test_false_error_cancellation_outcomes_are_success(self):
        for outcome in ("applied", "replayed"):
            with self.subTest(outcome=outcome):
                self.setUp_result_iteration()
                self.result = {"cancellation": outcome, "error": False, "order_id": "789"}
                self.invoke()
                self.assertEqual(self.row.state, "SUCCESS")
                self.assertEqual(self.store["effects"], ["producer"])

    def test_truthy_error_preserves_reconciliation_evidence(self):
        self.result = {"cancellation": "reconciliation", "error": "processed_history_requires_reconciliation"}
        self.invoke()
        self.assertEqual(self.row.state, "FAILED")
        self.assertEqual(self.row.processing_errors, self.result["error"])
        self.assertEqual(self.store["effects"], ["producer"], "A normal reconciliation result is not rollback")

    def test_ordinary_exception_rolls_back_producer_only(self):
        self.failure = RuntimeError("synthetic producer failure")
        self.invoke()
        self.assertEqual(self.row.state, "FAILED")
        self.assertEqual(self.store["effects"], [])

    def assert_marked_exception(self, marker):
        self.failure = RuntimeError("synthetic marked SQL-conflict transport")
        setattr(self.failure, marker, True)
        with self.assertRaises(RuntimeError) as caught:
            self.functions["_process_notification_order"](self.records)
        self.assertIs(caught.exception, self.failure)
        self.assertTrue(getattr(caught.exception, marker))
        self.assertEqual(self.store, {"caller": "preserved", "effects": []})
        self.assertEqual(self.cr.global_boundaries, [])
        self.assertNotEqual(self.row.state, "SUCCESS")

    def test_individual_marked_exception_preserves_identity(self):
        self.assert_marked_exception("_meli_individual_operation_failure")

    def test_pack_marked_exception_preserves_identity(self):
        self.assert_marked_exception("_meli_pack_operation_failure")

    def test_success_state_write_failure_rolls_back_producer(self):
        self.row.fault = "state"
        self.invoke()
        self.assertEqual(self.row.state, "FAILED")
        self.assertEqual(self.store["effects"], [])

    def test_ended_write_failure_rolls_back_producer(self):
        self.row.fault = "processing_ended"
        self.invoke()
        self.assertEqual(self.row.state, "FAILED")
        self.assertEqual(self.store["effects"], [])


if __name__ == "__main__":
    unittest.main()
