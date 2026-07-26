# -*- coding: utf-8 -*-
"""An invalid MercadoLibre session must be detected by HTTP status, not by
guessing from the response body.

``get_new_instance`` probes ``GET /users/{seller_id}`` to decide whether the
stored credentials still work, and decides by inspecting keys of the parsed
body:

    status = "status" in rjson and rjson["status"]     # a BODY field, not HTTP
    ...
    if (rjson and "error" in rjson) or refresh_force==True:

That works for the SDK backend, whose wrapper synthesises ``{"error": ...,
"status": <http status>}`` when the SDK raises. It does NOT work for the
requests backend, which returns MercadoLibre's raw JSON body — and a real 401
from ML carries neither ``error`` nor ``status``. The probe then falls through
as if the session were healthy, ``need_login()`` answers False, and every caller
proceeds with a dead token. Observed in production: the import ran against a
401 and blew up later on ``rjson3['status']``, taking a ``MeliRollback`` with it.

The requests backend is the deployed default, so this is the live path.

IMPORTANT for these tests: ``get_new_instance`` has a second, independent reason
to demand a login::

    right_access_token = ("-" + str(seller_id)) in str(access_token)
    if not right_access_token:
        needlogin_state = True

A token that does not embed the seller id would make ``need_login()`` answer
True for a reason that has nothing to do with the HTTP status, and the tests
below would pass against the broken code. Every token here therefore embeds the
seller id on purpose, so the ONLY thing under test is the status code.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.meli_oerp.models import meli_util
from odoo.addons.meli_oerp.models.meli_util import (
    MeliApiNoSDK,
    MeliConfiguration,
    configuration_nosdk,
)

_SELLER_ID = "2288636236"
# Embeds "-<seller_id>" so `right_access_token` is True: see the module
# docstring. Without this the tests would be green for the wrong reason.
_TOKEN_OK_FORMAT = "APP_USR-1234567890123456-072600-abcdef0123456789-%s" % _SELLER_ID

# A real 401 from MercadoLibre on this endpoint carries neither key.
_BODY_401_BARE = {"message": "invalid access token"}


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def _record(self, method, url, kwargs):
        self.calls.append({"method": method, "url": url})
        return self.response

    def get(self, url, **kwargs):
        return self._record("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, kwargs)

    def put(self, url, **kwargs):
        return self._record("PUT", url, kwargs)

    def delete(self, url, **kwargs):
        return self._record("DELETE", url, kwargs)


@tagged("post_install", "-at_install")
class TestLastStatusCodeIsRecorded(TransactionCase):
    """The client must keep the HTTP status of its last call.

    Today the status is read for logging inside ``get()`` and then discarded, so
    no caller can act on it.
    """

    def _client(self, response):
        client = MeliApiNoSDK(config=configuration_nosdk)
        client._session = _FakeSession(response)
        client.access_token = _TOKEN_OK_FORMAT
        client.seller_id = _SELLER_ID
        return client

    def test_status_is_recorded_for_every_outcome(self):
        for status in (200, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                c = self._client(_FakeResponse(status_code=status, json_data={}))
                c.get("/users/%s" % _SELLER_ID, {"access_token": _TOKEN_OK_FORMAT})
                self.assertEqual(
                    c.last_status_code, status,
                    "the HTTP status of the last call was not kept",
                )

    def test_body_pass_through_is_unchanged(self):
        """The existing backend contract must not move."""
        body = {"message": "invalid access token"}
        c = self._client(_FakeResponse(status_code=401, json_data=body))
        result = c.get("/users/%s" % _SELLER_ID, {"access_token": _TOKEN_OK_FORMAT})

        self.assertIs(result, c, "get() must still return self")
        self.assertEqual(c.rjson, body, "the body must still be passed through")


@tagged("post_install", "-at_install")
class TestSessionInvalidatedByHttpStatus(TransactionCase):
    """The real defect: the identity probe must believe the HTTP status."""

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER_ID,
            "mercadolibre_access_token": _TOKEN_OK_FORMAT,
            "mercadolibre_refresh_token": "TG-FAKE-REFRESH-%s" % _SELLER_ID,
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": "FAKE-SECRET",
        })

    def _probe(self, status_code, body):
        """Run get_new_instance with the identity probe answering as told."""
        session = _FakeSession(_FakeResponse(status_code=status_code, json_data=body))
        with patch.object(
            MeliConfiguration, "get_session", return_value=session
        ), patch.object(meli_util._versions, "USE_MELI_SDK", False):
            client = self.env["meli.util"].get_new_instance(self.company)
        return client, session

    def _assert_probe_ran(self, session):
        self.assertTrue(
            any("/users/%s" % _SELLER_ID in c["url"] for c in session.calls),
            "the identity probe was never issued",
        )

    def test_401_without_error_or_status_in_body_requires_login(self):
        """Main regression, reproducing production.

        Well-formed token, HTTP 401, and a body carrying neither ``error`` nor
        ``status``: the only usable signal is the status code.
        """
        self.assertNotIn("error", _BODY_401_BARE)
        self.assertNotIn("status", _BODY_401_BARE)

        client, session = self._probe(401, _BODY_401_BARE)

        self._assert_probe_ran(session)
        self.assertTrue(
            client.need_login(),
            "a 401 on the identity probe was not treated as an invalid session",
        )

    def test_403_without_error_or_status_in_body_requires_login(self):
        body = {"message": "forbidden"}
        self.assertNotIn("error", body)
        self.assertNotIn("status", body)

        client, session = self._probe(403, body)

        self._assert_probe_ran(session)
        self.assertTrue(
            client.need_login(),
            "a 403 on the identity probe was not treated as an invalid session",
        )

    def test_the_token_format_check_is_not_what_makes_these_pass(self):
        """Guard for the guard.

        ``right_access_token`` is a second, independent path to need_login().
        If the fixture token stopped embedding the seller id, the two tests
        above would pass against the broken code and prove nothing.
        """
        self.assertIn(
            "-%s" % _SELLER_ID, _TOKEN_OK_FORMAT,
            "the fixture token must embed the seller id, or the 401/403 tests "
            "would be green for the wrong reason",
        )

    def test_healthy_session_does_not_require_login(self):
        """200 must stay untouched."""
        body = {"id": int(_SELLER_ID), "nickname": "TESTSELLER", "tags": []}

        client, session = self._probe(200, body)

        self._assert_probe_ran(session)
        self.assertFalse(
            client.need_login(),
            "a healthy identity probe must not demand a login",
        )
