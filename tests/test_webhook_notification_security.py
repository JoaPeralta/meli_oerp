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
