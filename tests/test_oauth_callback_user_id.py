# -*- coding: utf-8 -*-
"""The OAuth callback must know whose credentials it just received.

THE DEFECT
----------
The callback exchanged the authorization code and stored whatever came back:

    resp = meli.authorize(code, redirect_uri)
    token_vals = {'mercadolibre_access_token': meli.access_token,
                  'mercadolibre_refresh_token': meli.refresh_token, ...}
    company.write(token_vals)

Nothing checked *which MercadoLibre account* the response belonged to. A user
who happened to be logged into a different seller account when the redirect
came back would have that account's credentials written over this company's,
silently. From then on every read, every import and every price would be read
from the wrong seller.

The refresh path has validated this since the token-validation change. The code
exchange did not.

THE CONTRACT
------------
The configured seller is a precondition, not something the callback discovers.
Before anything is persisted:

    seller configured        otherwise reject; no auto-linking
    user_id present          otherwise reject
    user_id normalisable     "123" and 123 are the same seller, "abc" is not
    user_id == that seller   otherwise reject
    access and refresh token both present and non-empty

Any failure: reject, write nothing at all, and say so without echoing a single
credential value.

Accounts are never linked automatically. Binding a company to whichever seller
happened to answer is exactly the confusion this prevents.
"""

import re
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

_SELLER = "2288636236"
_OTHER_SELLER = "9999999999"
_FAKE_CODE = "TG-FAKE-AUTH-CODE-0001"
_FAKE_ACCESS = "APPUSR-FAKE-ACCESS-0001-%s" % _SELLER
_FAKE_REFRESH = "TG-FAKE-REFRESH-0001"
_STORED_ACCESS = "PREEXISTING-ACCESS-0000"
_STORED_REFRESH = "PREEXISTING-REFRESH-0000"


class _FakeMeli:
    """Stands in for the client the callback builds. Never touches a network."""

    def __init__(self, payload):
        self._payload = payload
        self.access_token = payload.get("access_token") or ""
        self.refresh_token = payload.get("refresh_token") or ""
        self.authorize_calls = 0

    def authorize(self, code, redirect_uri=None):
        self.authorize_calls += 1
        return self._payload

    def auth_url(self, redirect_URI=None):
        return "https://auth.example/authorization"

    def get(self, path, params=None, **kwargs):
        # Resolving AUTH_URL probes /sites; answering nothing keeps this test
        # off the network and falls back to the currency lookup.
        return None


def _payload(**over):
    data = {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH,
            "token_type": "Bearer", "expires_in": 21600,
            "user_id": int(_SELLER)}
    data.update(over)
    return data


@tagged("post_install", "-at_install")
class TestOAuthCallbackUserId(HttpCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.company.write({
            "mercadolibre_redirect_uri": "https://example.test/meli_login",
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": "client-secret-value-4444444444",
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _STORED_ACCESS,
            "mercadolibre_refresh_token": _STORED_REFRESH,
        })
        self.env.flush_all()

    # ------------------------------------------------------------------
    def _stored(self):
        """Straight from SQL, past every ORM cache."""
        self.env.cr.execute(
            "SELECT access_token, refresh_token FROM mercadolibre_auth "
            "WHERE company_id = %s", (self.company.id,))
        row = self.env.cr.fetchone()
        return row if row else (None, None)

    def _callback(self, payload):
        """Drives the real flow: get a login link, then come back with its state.

        The callback refuses any code it cannot match to an authorization
        request from this session, so these tests have to ask for one first.
        """
        fake = _FakeMeli(payload)
        self.authenticate("admin", "admin")
        with patch.object(type(self.env["meli.util"]), "_build_client",
                          return_value=fake):
            issued = self.url_open("/meli_login", allow_redirects=False)
            self.assertEqual(
                issued.status_code, 200,
                "the login entry point failed (%s), so no state was issued and "
                "every assertion below would hold for the wrong reason"
                % issued.status_code)
            found = re.search(r"state=([A-Za-z0-9_\-]+)", issued.text)
            state = found.group(1) if found else ""
            response = self.url_open(
                "/meli_login?code=%s&state=%s" % (_FAKE_CODE, state),
                allow_redirects=False)
        self.env.invalidate_all()
        return response, fake

    def _assert_nothing_written(self, response, why):
        access, refresh = self._stored()
        self.assertEqual(access, _STORED_ACCESS,
                         "%s: the stored access token was overwritten" % why)
        self.assertEqual(refresh, _STORED_REFRESH,
                         "%s: the stored refresh token was overwritten" % why)
        body = response.text
        for secret in (_FAKE_ACCESS, _FAKE_REFRESH, _FAKE_CODE):
            self.assertNotIn(secret, body,
                             "%s: a credential leaked into the response" % why)

    # ------------------------------------------------------------------
    # the happy path still works
    # ------------------------------------------------------------------
    def test_a_matching_user_id_is_persisted(self):
        response, fake = self._callback(_payload())

        self.assertEqual(fake.authorize_calls, 1)
        access, refresh = self._stored()
        self.assertEqual(access, _FAKE_ACCESS)
        self.assertEqual(refresh, _FAKE_REFRESH)
        self.assertNotIn(_FAKE_ACCESS, response.text,
                         "the access token was rendered in the response")

    def test_a_string_user_id_matching_the_seller_is_accepted(self):
        """MercadoLibre reports it as a number; a string of the same digits is
        the same seller and must not be rejected on type alone."""
        self._callback(_payload(user_id=_SELLER))

        self.assertEqual(self._stored()[0], _FAKE_ACCESS)

    # ------------------------------------------------------------------
    # rejections
    # ------------------------------------------------------------------
    def test_a_different_user_id_is_rejected_and_writes_nothing(self):
        """The whole point: another account's credentials must never land here."""
        response, _fake = self._callback(_payload(user_id=int(_OTHER_SELLER)))

        self._assert_nothing_written(
            response, "credentials issued for a different MercadoLibre account")

    def test_a_missing_user_id_is_rejected_and_writes_nothing(self):
        payload = _payload()
        del payload["user_id"]

        response, _fake = self._callback(payload)

        self._assert_nothing_written(response, "a response with no user_id")

    def test_an_unusable_user_id_type_is_rejected(self):
        response, _fake = self._callback(_payload(user_id="not-a-number"))

        self._assert_nothing_written(response, "a non-numeric user_id")

    def test_a_null_user_id_is_rejected(self):
        response, _fake = self._callback(_payload(user_id=None))

        self._assert_nothing_written(response, "a null user_id")

    def test_a_response_without_a_refresh_token_is_rejected(self):
        payload = _payload()
        del payload["refresh_token"]

        response, _fake = self._callback(payload)

        self._assert_nothing_written(
            response, "a response carrying no refresh token")

    def test_a_response_with_an_empty_access_token_is_rejected(self):
        response, _fake = self._callback(_payload(access_token=""))

        self._assert_nothing_written(response, "an empty access token")

    # ------------------------------------------------------------------
    # the seller is a precondition, not something to discover
    # ------------------------------------------------------------------
    def test_no_configured_seller_is_rejected_and_never_auto_links(self):
        """Binding the company to whoever answered is exactly the confusion
        this validation exists to prevent."""
        self.company.write({"mercadolibre_seller_id": False})
        self.env.flush_all()

        response, _fake = self._callback(_payload())

        self._assert_nothing_written(response, "no configured seller")
        self.env.invalidate_all()
        self.assertFalse(
            self.company.mercadolibre_seller_id,
            "the callback bound the company to the seller that answered")

    # ------------------------------------------------------------------
    # atomicity
    # ------------------------------------------------------------------
    def test_a_failure_while_persisting_leaves_nothing_behind(self):
        """No half-written credential pair."""
        original_write = type(self.company).write

        def exploding_write(self_recordset, vals):
            if "mercadolibre_access_token" in vals:
                raise ValueError("persistence blew up")
            return original_write(self_recordset, vals)

        fake = _FakeMeli(_payload())
        self.authenticate("admin", "admin")
        with patch.object(type(self.env["meli.util"]), "_build_client",
                          return_value=fake), \
                patch.object(type(self.company), "write", exploding_write):
            issued = self.url_open("/meli_login", allow_redirects=False)
            found = re.search(r"state=([A-Za-z0-9_\-]+)", issued.text)
            state = found.group(1) if found else ""
            self.url_open("/meli_login?code=%s&state=%s" % (_FAKE_CODE, state),
                          allow_redirects=False)
        self.env.invalidate_all()

        access, refresh = self._stored()
        self.assertEqual(access, _STORED_ACCESS,
                         "a failed persist left a partial credential behind")
        self.assertEqual(refresh, _STORED_REFRESH)
