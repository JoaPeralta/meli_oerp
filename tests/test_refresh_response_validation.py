# -*- coding: utf-8 -*-
"""A refresh response must be validated before its credentials are trusted.

Today the only check is ``if "access_token" in refjson``. Everything else is
taken on faith:

    api_rest_client.access_token  = refjson["access_token"]
    api_rest_client.refresh_token = refjson["refresh_token"]   # KeyError if absent

Three ways that goes wrong:

* **``refresh_token`` missing.** ``get_refresh_token`` already did
  ``self.refresh_token = response_info.get('refresh_token', '')`` — it replaced
  the working refresh token with an **empty string**. The next renewal then has
  nothing to send, and since MercadoLibre's refresh tokens are single-use the
  previous one is already spent. The session is unrecoverable without a manual
  OAuth round.

* **``user_id`` not checked.** A response for a different account would be
  accepted and stored as this company's credentials.

* **empty strings.** ``"access_token" in refjson`` is true for
  ``{"access_token": ""}``.

The rule adopted here: a refresh is successful only if the response carries a
non-empty ``access_token``, a non-empty ``refresh_token``, and a ``user_id``
matching the configured seller. Anything else leaves the stored credentials
untouched — a bad refresh must not be able to destroy a working session.

Scope: validating the response. Serialising concurrent refreshes and the
transaction boundary are separate changes.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.meli_oerp.models.meli_util import MeliConfiguration

_SELLER = "2288636236"
_OTHER_SELLER = "9999999999"
_OLD_ACCESS = "OLD-ACCESS-value-000000000000-%s" % _SELLER
_OLD_REFRESH = "OLD-REFRESH-value-111111111111"
_NEW_ACCESS = "NEW-ACCESS-value-222222222222-%s" % _SELLER
_NEW_REFRESH = "NEW-REFRESH-value-333333333333"


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, post_payload):
        self.post_payload = post_payload

    def get(self, url, **kwargs):
        return _Resp(401, {"error": "invalid_token",
                           "message": "invalid or expired token", "status": 401})

    def post(self, url, **kwargs):
        return _Resp(200, self.post_payload)

    def put(self, url, **kwargs):
        return _Resp(200, {})

    def delete(self, url, **kwargs):
        return _Resp(200, {})


@tagged("post_install", "-at_install")
class TestRefreshResponseValidation(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _OLD_ACCESS,
            "mercadolibre_refresh_token": _OLD_REFRESH,
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": "client-secret-value-4444444444",
            "mercadolibre_cron_refresh": True,
        })

    def _refresh_with(self, payload):
        session = _Session(payload)
        with patch.object(MeliConfiguration, "get_session", return_value=session):
            client = self.env["meli.util"].get_new_instance(self.company)
        self.company.invalidate_recordset()
        return client

    def _good(self, **over):
        p = {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH,
             "token_type": "Bearer", "expires_in": 21600, "user_id": int(_SELLER)}
        p.update(over)
        return p

    def _assert_credentials_untouched(self, why):
        self.assertEqual(self.company.mercadolibre_access_token, _OLD_ACCESS, why)
        self.assertEqual(self.company.mercadolibre_refresh_token, _OLD_REFRESH, why)

    # ------------------------------------------------------------------
    # the happy path still works
    # ------------------------------------------------------------------
    def test_a_valid_response_is_accepted(self):
        self._refresh_with(self._good())

        self.assertEqual(self.company.mercadolibre_access_token, _NEW_ACCESS)
        self.assertEqual(self.company.mercadolibre_refresh_token, _NEW_REFRESH)

    # ------------------------------------------------------------------
    # incomplete responses
    # ------------------------------------------------------------------
    def test_response_without_refresh_token_is_rejected(self):
        """The dangerous one: it used to blank the working refresh token."""
        payload = self._good()
        del payload["refresh_token"]

        self._refresh_with(payload)

        self._assert_credentials_untouched(
            "a response without refresh_token overwrote the stored credentials; "
            "the previous refresh token is single-use and already spent")
        self.assertTrue(
            self.company.mercadolibre_refresh_token,
            "the refresh token was blanked: the session cannot be renewed again")

    def test_response_with_empty_refresh_token_is_rejected(self):
        self._refresh_with(self._good(refresh_token=""))
        self._assert_credentials_untouched("an empty refresh_token was accepted")

    def test_response_with_empty_access_token_is_rejected(self):
        self._refresh_with(self._good(access_token=""))
        self._assert_credentials_untouched("an empty access_token was accepted")

    # ------------------------------------------------------------------
    # wrong account
    # ------------------------------------------------------------------
    def test_response_for_another_seller_is_rejected(self):
        """Credentials belonging to a different account must never be stored."""
        self._refresh_with(self._good(user_id=int(_OTHER_SELLER)))

        self._assert_credentials_untouched(
            "credentials issued for a different MercadoLibre account were "
            "stored as this company's")

    def test_response_without_user_id_is_rejected(self):
        payload = self._good()
        del payload["user_id"]

        self._refresh_with(payload)

        self._assert_credentials_untouched(
            "a response with no user_id was accepted without verifying the "
            "account it belongs to")

    # ------------------------------------------------------------------
    # error responses
    # ------------------------------------------------------------------
    def test_invalid_grant_leaves_credentials_alone(self):
        self._refresh_with({"error": "invalid_grant",
                            "message": "refresh token expired"})
        self._assert_credentials_untouched("invalid_grant modified the credentials")

    def test_rejected_refresh_does_not_stamp_expiry_metadata(self):
        """A rejected refresh must not look like a fresh one."""
        payload = self._good()
        del payload["refresh_token"]

        self._refresh_with(payload)

        self.assertFalse(
            self.company.mercadolibre_token_refreshed_at,
            "a rejected refresh stamped the expiry metadata as if it had worked")
