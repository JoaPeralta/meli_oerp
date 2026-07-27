# -*- coding: utf-8 -*-
"""The /oauth/token response must never reach logs or the database.

The refresh branch of ``get_new_instance`` writes the whole token response to
two destinations:

    _logger.info("Refresh result: " + str(refresh))     # Odoo log / stderr
    logs += str(refjson) + "\\n"                         # -> mercadolibre.notification

``refresh`` is the parsed body of ``POST /oauth/token``, which carries the new
**access_token and refresh_token in clear text**. The notification row persists
them in the database; the log line ships them wherever the platform collects
stderr — for this deployment, Railway.

The exception path is no safer: ``errors += str(e)`` and ``_logger.error(e)``
put an arbitrary exception string in both places, and the client is holding the
credentials at that moment.

A historical audit found zero actual exposure — the refresh branch never ran,
because it is gated on body keys a real MercadoLibre 401 does not carry. That
is luck, not safety: the moment refresh starts working, every renewal leaks.

Scope: the leak only. The refresh protocol itself (single-use handling,
expiry metadata, validation, locking, transaction boundary) is TD8 and is
addressed in its own changes.
"""

import logging

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.meli_oerp.models.meli_util import MeliConfiguration

_SELLER = "2288636236"
# The stored token must embed the seller id or get_new_instance bails out early
# on `right_access_token` and never reaches the refresh branch.
_OLD_ACCESS = "SENTINEL-OLD-ACCESS-aaaaaaaaaaaaaaaa-%s" % _SELLER
_OLD_REFRESH = "SENTINEL-OLD-REFRESH-bbbbbbbbbbbbbbbb"
_CLIENT_SECRET = "SENTINEL-CLIENT-SECRET-cccccccccccccccc"
# What MercadoLibre would hand back.
_NEW_ACCESS = "SENTINEL-NEW-ACCESS-dddddddddddddddd-%s" % _SELLER
_NEW_REFRESH = "SENTINEL-NEW-REFRESH-eeeeeeeeeeeeeeee"

_ALL_SENTINELS = [_OLD_ACCESS, _OLD_REFRESH, _CLIENT_SECRET,
                  _NEW_ACCESS, _NEW_REFRESH]

_LOGGERS = ["odoo.addons.meli_oerp.models.meli_util"]


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    """GET answers the identity probe; POST answers /oauth/token."""

    def __init__(self, post_payload=None, post_exc=None):
        self.post_payload = post_payload
        self.post_exc = post_exc
        self.posts = []

    def get(self, url, **kwargs):
        # A body that drives get_new_instance into its refresh branch.
        return _Resp(401, {"error": "invalid_token",
                           "message": "invalid or expired token",
                           "status": 401})

    def post(self, url, **kwargs):
        self.posts.append(url)
        if self.post_exc is not None:
            raise self.post_exc
        return _Resp(200, self.post_payload)

    def put(self, url, **kwargs):
        return _Resp(200, {})

    def delete(self, url, **kwargs):
        return _Resp(200, {})


@tagged("post_install", "-at_install")
class TestNoTokenSecretsInLogs(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _OLD_ACCESS,
            "mercadolibre_refresh_token": _OLD_REFRESH,
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": _CLIENT_SECRET,
            "mercadolibre_cron_refresh": True,
        })
        self.Noti = self.env["mercadolibre.notification"]
        self.notis_before = set(self.Noti.search([]).ids)

    def _run(self, post_payload=None, post_exc=None):
        """Drive the real refresh branch and capture everything it logs."""
        session = _Session(post_payload=post_payload, post_exc=post_exc)
        with patch.object(MeliConfiguration, "get_session", return_value=session):
            with self.assertLogs(_LOGGERS[0], level="DEBUG") as captured:
                self.env["meli.util"].get_new_instance(self.company)
        self.session = session
        return "\n".join(r.getMessage() for r in captured.records)

    def _notification_text(self):
        """Everything the run wrote into notification rows."""
        new = self.Noti.search([("id", "not in", list(self.notis_before))])
        return "\n".join(
            (n.processing_logs or "") + "\n" + (n.processing_errors or "")
            for n in new)

    def _assert_no_sentinel(self, text, where):
        found = [s.split("-")[1] + "-" + s.split("-")[2]
                 for s in _ALL_SENTINELS if s in text]
        self.assertEqual(
            found, [],
            "credentials leaked into %s: %s" % (where, found))

    # ------------------------------------------------------------------
    # successful refresh
    # ------------------------------------------------------------------
    def _ok_payload(self):
        return {
            "access_token": _NEW_ACCESS,
            "refresh_token": _NEW_REFRESH,
            "token_type": "Bearer",
            "expires_in": 21600,
            "scope": "offline_access read write",
            "user_id": int(_SELLER),
        }

    def test_refresh_branch_actually_runs(self):
        """Guard: the assertions below prove nothing if no POST happened."""
        self._run(post_payload=self._ok_payload())

        self.assertTrue(
            any("/oauth/token" in u for u in self.session.posts),
            "the refresh branch was never reached; this fixture no longer "
            "drives it and every other assertion here is vacuous",
        )

    def test_access_token_never_reaches_the_log(self):
        text = self._run(post_payload=self._ok_payload())
        self.assertIn(_NEW_ACCESS, str(self._ok_payload()))   # sanity
        self._assert_no_sentinel(text, "the log")

    def test_refresh_token_never_reaches_the_log(self):
        text = self._run(post_payload=self._ok_payload())
        self.assertNotIn(_NEW_REFRESH, text)
        self.assertNotIn(_OLD_REFRESH, text)

    def test_client_secret_never_reaches_the_log(self):
        text = self._run(post_payload=self._ok_payload())
        self.assertNotIn(_CLIENT_SECRET, text)

    def test_secrets_never_reach_mercadolibre_notification(self):
        self._run(post_payload=self._ok_payload())
        self._assert_no_sentinel(self._notification_text(), "mercadolibre.notification")

    # ------------------------------------------------------------------
    # failure and exception paths
    # ------------------------------------------------------------------
    def test_error_response_does_not_leak(self):
        """invalid_grant: the response has no tokens, but the client holds them."""
        text = self._run(post_payload={"error": "invalid_grant",
                                       "message": "refresh token expired"})
        self._assert_no_sentinel(text, "the log (error response)")
        self._assert_no_sentinel(self._notification_text(),
                                 "mercadolibre.notification (error response)")

    def test_exception_path_does_not_leak(self):
        """An exception string must not carry credentials either."""
        import requests
        boom = requests.ConnectionError(
            "connection failed while posting token=%s secret=%s"
            % (_OLD_REFRESH, _CLIENT_SECRET))

        text = self._run(post_exc=boom)

        self._assert_no_sentinel(text, "the log (exception path)")
        self._assert_no_sentinel(self._notification_text(),
                                 "mercadolibre.notification (exception path)")

    # ------------------------------------------------------------------
    # the fix must not break the refresh
    # ------------------------------------------------------------------
    def test_successful_refresh_still_stores_the_new_credentials(self):
        """Redacting the logs must not change what gets persisted."""
        self._run(post_payload=self._ok_payload())

        self.assertEqual(self.company.mercadolibre_access_token, _NEW_ACCESS)
        self.assertEqual(self.company.mercadolibre_refresh_token, _NEW_REFRESH)
