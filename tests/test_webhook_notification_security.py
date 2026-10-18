# -*- coding: utf-8 -*-
"""Integration regression test: a webhook-controlled ``resource`` must never
cause an authenticated request to an attacker-chosen host.

This test replaces the MercadoLibre client returned by
``meli.util.get_new_instance`` with a fake that records every path it is asked
to fetch. It then processes notifications whose ``resource`` is malicious and
asserts that the fake client is NEVER called (so no ``Authorization: Bearer``
token is ever sent anywhere), while a legitimate ``/orders/...`` resource does
reach the client.

Requires a running Odoo test environment; it uses no network and no
credentials.
"""

from unittest.mock import patch

from psycopg2.errors import DeadlockDetected, SerializationFailure
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


class _FakeMeliResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeMeli:
    """Records fetched paths; returns an empty/harmless payload."""

    def __init__(self):
        self.access_token = "TEST-TOKEN-should-never-leave"
        self.calls = []

    def get(self, path, params=None, extra_headers=None, **kwargs):
        self.calls.append(path)
        return _FakeMeliResponse({})

    # methods some processors may touch; harmless no-ops
    def get_mini(self, path, params=None, extra_headers=None, **kwargs):
        self.calls.append(path)
        return _FakeMeliResponse({})


@tagged("post_install", "-at_install")
class TestWebhookNotificationSecurity(TransactionCase):

    def _make_notification(self, topic, resource):
        return self.env["mercadolibre.notification"].create(
            {
                "notification_id": "test-%s-%s" % (topic, abs(hash(resource)) % 10_000),
                "topic": topic,
                "resource": resource,
                "state": "RECEIVED",
                "application_id": "0",
                "user_id": "0",
            }
        )

    def test_malicious_order_resource_does_not_call_client(self):
        fake = _FakeMeli()
        noti = self._make_notification("orders_v2", "https://attacker.example/steal")
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ):
            noti._process_notification_order()
        self.assertEqual(
            fake.calls,
            [],
            "A malicious webhook resource reached the HTTP client: the Bearer "
            "token could be exfiltrated.",
        )
        self.assertEqual(noti.state, "FAILED")

    def test_scheme_relative_order_resource_does_not_call_client(self):
        fake = _FakeMeli()
        noti = self._make_notification("orders_v2", "//attacker.example/steal")
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ):
            noti._process_notification_order()
        self.assertEqual(fake.calls, [])
        self.assertEqual(noti.state, "FAILED")

    def test_malicious_question_resource_does_not_call_client(self):
        fake = _FakeMeli()
        noti = self._make_notification("questions", "http://attacker.example/steal")
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ):
            noti._process_notification_question()
        self.assertEqual(fake.calls, [])
        self.assertEqual(noti.state, "FAILED")

    def test_legit_order_resource_reaches_client(self):
        fake = _FakeMeli()
        noti = self._make_notification("orders_v2", "/orders/2000000000000000")
        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ):
            noti._process_notification_order()
        self.assertEqual(
            fake.calls,
            ["/orders/2000000000000000"],
            "A legitimate order resource must still be fetched from the API.",
        )


@tagged("post_install", "-at_install", "meli_legacy_order_results")
class TestLegacyOrderNotificationResults(TransactionCase):
    """Real savepoints/ACLs, historical principal and a synthetic producer.

    This is not an end-to-end commercial import or technical-actor outcome grant.
    """

    def setUp(self):
        super().setUp()
        user = self.env.ref("base.user_admin")
        self.legacy = self.env(user=user.id, su=False,
                               context=dict(self.env.context, allowed_company_ids=self.env.company.ids))
        self.assertFalse(self.legacy.su)
        group = self.env.ref("viarengo_circuit_meli.group_meli_integration", raise_if_not_found=False)
        if group:
            self.assertFalse(self.legacy.user.has_group("viarengo_circuit_meli.group_meli_integration"))
        self.marker = "legacy-result-" + self._testMethodName
        self.row = self.legacy["mercadolibre.notification"].create({
            "notification_id": self.marker, "topic": "orders_v2", "resource": "/orders/996669000001",
            "state": "RECEIVED", "application_id": "0", "user_id": "0"})
        self.sentinel = self.legacy["res.partner"].create({"name": self.marker + "-caller"})
        self.effects = []
        self.producer_calls = 0
        self.iteration = 0
        self.fake = _FakeMeli()
        self.fake.get = self.fake_get

    def fake_get(self, path, params=None, **kwargs):
        self.fake.calls.append(path)
        return _FakeMeliResponse({"id": 996669000001})

    def attempt(self, result=None, failure=None, fault=None, row=None):
        self.iteration += 1
        self.effects.clear()
        self.fake.calls.clear()
        self.producer_calls = 0
        target = row if row is not None else self.row
        native_write = type(self.row).write
        armed = [fault]

        def produce(records, data, context=None, config=None, meli=None):
            self.producer_calls += 1
            self.assertFalse(records.env.su)
            self.assertEqual(records.env.uid, self.legacy.uid)
            partner = records.env["res.partner"].create({"name": self.marker + "-effect-" + str(self.iteration)})
            self.effects.append(partner.id)
            if failure is not None:
                raise failure
            return result

        def outcome_write(records, values):
            matches = armed[0] == "state" and values.get("state") == "SUCCESS"
            matches = matches or armed[0] == "processing_ended" and "processing_ended" in values
            if self.row.id in records.ids and matches:
                armed[0] = None
                raise RuntimeError("synthetic outcome write failure")
            return native_write(records, values)

        with patch.object(type(self.legacy["meli.util"]), "get_new_instance", return_value=self.fake), \
                patch.object(type(self.legacy["mercadolibre.orders"]), "orders_update_order_json", produce), \
                patch.object(type(self.row), "write", outcome_write):
            target._process_notification_order()

    def assert_effects(self, retained):
        self.row.invalidate_recordset()
        self.assertTrue(self.sentinel.exists(), "Unrelated caller work must survive")
        self.assertEqual(bool(self.legacy["res.partner"].browse(self.effects).exists()), retained)
        self.assertEqual(self.fake.calls, ["/orders/996669000001"])
        self.assertEqual(self.producer_calls, 1)

    def test_false_and_none_results_roll_back_only_producer(self):
        for result in (False, None):
            with self.subTest(result=result):
                self.attempt(result)
                self.assert_effects(False)
                self.assertEqual(self.row.state, "FAILED")
                self.assertTrue(self.row.processing_errors)
                self.assertTrue(self.row.processing_ended)

    def test_non_dictionary_results_are_not_verified_success(self):
        for result in (True, 0, [], "unexpected"):
            with self.subTest(result=result):
                self.attempt(result)
                self.assert_effects(False)
                self.assertEqual(self.row.state, "FAILED")

    def test_empty_dictionary_keeps_legacy_success(self):
        self.attempt({})
        self.assert_effects(True)
        self.assertEqual(self.row.state, "SUCCESS")
        self.assertTrue(self.row.processing_ended)

    def test_false_error_cancellation_results_are_success(self):
        for outcome in ("applied", "replayed"):
            with self.subTest(outcome=outcome):
                self.attempt({"cancellation": outcome, "error": False})
                self.assert_effects(True)
                self.assertEqual(self.row.state, "SUCCESS")

    def test_reported_reconciliation_failure_preserves_local_evidence(self):
        self.attempt({"cancellation": "reconciliation", "error": "processed_history_requires_reconciliation"})
        self.assert_effects(True)
        self.assertEqual(self.row.state, "FAILED")
        self.assertEqual(self.row.processing_errors, "processed_history_requires_reconciliation")

    def test_ordinary_exception_rolls_back_only_producer(self):
        self.attempt(failure=RuntimeError("synthetic producer failure"))
        self.assert_effects(False)
        self.assertEqual(self.row.state, "FAILED")

    def test_marked_conflicts_escape_with_original_identity(self):
        # Injected native exception classes, not a real server race or pgcode claim.
        for kind, marker in ((SerializationFailure, "_meli_individual_operation_failure"),
                             (DeadlockDetected, "_meli_pack_operation_failure")):
            with self.subTest(marker=marker):
                failure = kind("synthetic marked conflict")
                setattr(failure, marker, True)
                with self.assertRaises(kind) as caught:
                    self.attempt(failure=failure)
                self.assertIs(caught.exception, failure)
                self.assertTrue(getattr(caught.exception, marker))
                self.assert_effects(False)
                self.assertNotEqual(self.row.state, "SUCCESS")

    def test_outcome_write_failure_rolls_back_producer(self):
        for field in ("state", "processing_ended"):
            with self.subTest(field=field):
                self.attempt({}, fault=field)
                self.assert_effects(False)
                self.assertEqual(self.row.state, "FAILED")

    def test_technical_actor_still_cannot_process_legacy_outcomes(self):
        actor = self.env.ref("viarengo_circuit_meli.user_meli_integration", raise_if_not_found=False)
        if not actor:
            self.skipTest("The optional bridge technical actor is not installed")
        target = self.row.with_user(actor).sudo(False)
        with self.assertRaises(AccessError):
            self.attempt({}, row=target)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.producer_calls, 0)
        self.assertTrue(self.sentinel.exists())
        self.row.invalidate_recordset()
        self.assertEqual(self.row.state, "RECEIVED")

    def test_new_inbox_receipt_is_denied_before_client(self):
        self.row.notification_id = "ml:legacy-result-synthetic"
        with patch.object(type(self.legacy["meli.util"]), "get_new_instance") as factory:
            with self.assertRaises(AccessError):
                self.row._process_notification_order()
        factory.assert_not_called()
        self.assertTrue(self.sentinel.exists())
