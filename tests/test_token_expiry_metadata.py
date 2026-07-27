# -*- coding: utf-8 -*-
"""The lifetime MercadoLibre reports must be stored, not guessed.

Every `/oauth/token` response carries ``expires_in`` and ``user_id``. The
connector reads neither: ``get_refresh_token`` keeps ``access_token`` and
``refresh_token`` and drops the rest, and ``res.company`` has no field to hold
an expiry at all.

The consequence is that the connector cannot answer "is this token still
usable?" without asking MercadoLibre, and cannot renew ahead of expiry at all.
It can only react to a 401 after the fact — in the middle of whatever operation
happened to hit it.

This adds the metadata and stores it at both places a token is obtained:

    POST /oauth/token  grant_type=authorization_code   (OAuth callback)
    POST /oauth/token  grant_type=refresh_token        (renewal)

Deliberately NOT hardcoding a TTL. The stored expiry comes from the
``expires_in`` MercadoLibre actually returned plus the moment we received it.
When a response omits ``expires_in``, nothing is invented: the expiry is left
unset and the caller must treat the lifetime as unknown.

Scope: capture and persistence. Using this to decide *when* to renew, and the
locking around the renewal itself, are separate changes.
"""

from datetime import datetime, timedelta

from unittest.mock import patch

from odoo import fields as odoo_fields
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.meli_oerp.models.meli_util import MeliConfiguration

_SELLER = "2288636236"
_OLD_ACCESS = "OLD-ACCESS-token-value-0000000000-%s" % _SELLER
_NEW_ACCESS = "NEW-ACCESS-token-value-1111111111-%s" % _SELLER
_NEW_REFRESH = "NEW-REFRESH-token-value-22222222"


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
class TestTokenExpiryMetadata(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _OLD_ACCESS,
            "mercadolibre_refresh_token": "OLD-REFRESH-token-value-33333333",
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": "client-secret-value-4444444444",
            "mercadolibre_cron_refresh": True,
        })

    def _refresh_with(self, payload):
        session = _Session(payload)
        with patch.object(MeliConfiguration, "get_session", return_value=session):
            self.env["meli.util"].get_new_instance(self.company)
        self.company.invalidate_recordset()

    def _payload(self, **over):
        p = {
            "access_token": _NEW_ACCESS,
            "refresh_token": _NEW_REFRESH,
            "token_type": "Bearer",
            "expires_in": 21600,
            "user_id": int(_SELLER),
        }
        p.update(over)
        return p

    # ------------------------------------------------------------------
    # the fields must exist at all
    # ------------------------------------------------------------------
    def test_company_carries_expiry_metadata_fields(self):
        fields = self.env["res.company"]._fields
        for name in ("mercadolibre_token_expires_in",
                     "mercadolibre_token_refreshed_at",
                     "mercadolibre_token_expires_at"):
            self.assertIn(
                name, fields,
                "res.company has no %s: the connector cannot know when the "
                "token expires" % name)

    # ------------------------------------------------------------------
    # capture on refresh
    # ------------------------------------------------------------------
    def test_refresh_stores_expires_in(self):
        self._refresh_with(self._payload())

        self.assertEqual(self.company.mercadolibre_access_token, _NEW_ACCESS)
        self.assertEqual(self.company.mercadolibre_token_expires_in, 21600)

    def test_refresh_stores_the_moment_it_happened(self):
        # Odoo persists datetimes in UTC, so the comparison has to use Odoo's
        # own clock rather than the container's local time.
        before = odoo_fields.Datetime.now()
        self._refresh_with(self._payload())

        stamp = self.company.mercadolibre_token_refreshed_at
        self.assertTrue(stamp, "the refresh instant was not recorded")
        self.assertGreaterEqual(stamp, before - timedelta(seconds=5))
        self.assertLessEqual(stamp, odoo_fields.Datetime.now() + timedelta(seconds=5))

    def test_expires_at_is_derived_from_the_reported_lifetime(self):
        """No hardcoded TTL: expires_at = refreshed_at + expires_in."""
        self._refresh_with(self._payload(expires_in=1800))

        stamp = self.company.mercadolibre_token_refreshed_at
        expires = self.company.mercadolibre_token_expires_at
        self.assertTrue(expires, "expires_at was not derived")
        delta = (expires - stamp).total_seconds()
        self.assertAlmostEqual(
            delta, 1800, delta=5,
            msg="expires_at does not match the lifetime MercadoLibre reported")

    def test_a_different_lifetime_is_honoured(self):
        """Whatever ML says is what gets stored — 6 h is not baked in."""
        self._refresh_with(self._payload(expires_in=900))

        self.assertEqual(self.company.mercadolibre_token_expires_in, 900)
        delta = (self.company.mercadolibre_token_expires_at
                 - self.company.mercadolibre_token_refreshed_at).total_seconds()
        self.assertAlmostEqual(delta, 900, delta=5)

    def test_missing_expires_in_is_not_invented(self):
        """An absent lifetime must leave the expiry unknown, not guessed."""
        payload = self._payload()
        del payload["expires_in"]

        self._refresh_with(payload)

        self.assertEqual(self.company.mercadolibre_access_token, _NEW_ACCESS)
        self.assertFalse(
            self.company.mercadolibre_token_expires_at,
            "an expiry was invented for a response without expires_in")

    # ------------------------------------------------------------------
    # the metadata must not survive a failed refresh
    # ------------------------------------------------------------------
    def test_failed_refresh_does_not_touch_the_metadata(self):
        self._refresh_with(self._payload(expires_in=1800))
        stamp = self.company.mercadolibre_token_refreshed_at
        expires_in = self.company.mercadolibre_token_expires_in

        self._refresh_with({"error": "invalid_grant",
                            "message": "refresh token expired"})

        self.assertEqual(self.company.mercadolibre_token_refreshed_at, stamp)
        self.assertEqual(self.company.mercadolibre_token_expires_in, expires_in)
