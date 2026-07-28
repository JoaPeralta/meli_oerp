# -*- coding: utf-8 -*-
"""Building a client must never rotate credentials, and renewing must be a
decision, not a side effect of an error body.

THE DEFECT
----------
``get_new_instance`` both constructs the client and, on its way, may renew the
credentials. Worse, the renewal was gated on the *shape of an error body*:

    if (rjson and "error" in rjson) or refresh_force:
        ...
        elif refresh_force or "invalid" in message or "expired" in message ...:
            refresh

So a 403, a gateway timeout, a 5xx or any payload that happens to carry an
``error`` key could reach the renewal path, while a real MercadoLibre 401 --
which carries no ``error`` key at all -- could not. The trigger had nothing to
do with whether the token was actually expired.

THE CONTRACT
------------
``_build_client(company)`` is a pure constructor: no network, no renewal, no
writes. Anything that only needs a client object uses it.

``get_new_instance`` stays the authenticated boundary and may call the TD8
primitive **at most once**, on exactly three triggers:

    refresh_force            an explicit request
    expiry metadata due      what MercadoLibre itself reported
    identity probe HTTP 401  the session is demonstrably unusable

and on nothing else. A 403, a timeout, an unparseable body, a 5xx or the mere
presence of an ``error`` key must never renew: none of them says the token
expired, and each renewal spends a single-use credential.

After a renewal that produced usable credentials, the identity is validated
once. Never a second renewal in the same call.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.meli_oerp.models.meli_util import MeliConfiguration
from odoo.addons.meli_oerp.tests.meli_auth_test_cursor import AmbientAuthCursor

_SELLER = "2288636236"
_OLD_ACCESS = "OLD-ACCESS-000000000000-%s" % _SELLER
_OLD_REFRESH = "OLD-REFRESH-111111111111"
_NEW_ACCESS = "NEW-ACCESS-222222222222-%s" % _SELLER
_NEW_REFRESH = "NEW-REFRESH-333333333333"

_TOKEN_PATH = "/oauth/token"


class _Resp:
    def __init__(self, status_code, payload, parseable=True):
        self.status_code = status_code
        self._payload = payload
        self._parseable = parseable
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        if not self._parseable:
            raise ValueError("no json")
        return self._payload


class _CountingSession:
    """Counts every POST to the token endpoint. Nothing here reaches a network."""

    def __init__(self, get_response, token_payload=None):
        self.oauth_post_count = 0
        self.get_count = 0
        self._get_response = get_response
        self._token_payload = token_payload or {
            "access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH,
            "token_type": "Bearer", "expires_in": 21600,
            "user_id": int(_SELLER)}

    def get(self, url, **kwargs):
        self.get_count += 1
        response = self._get_response
        if callable(response):
            response = response(self.get_count)
        return response

    def post(self, url, **kwargs):
        if _TOKEN_PATH in str(url):
            self.oauth_post_count += 1
            return _Resp(200, self._token_payload)
        return _Resp(200, {})

    def put(self, url, **kwargs):
        return _Resp(200, {})

    def delete(self, url, **kwargs):
        return _Resp(200, {})


@tagged("post_install", "-at_install")
class TestNoHiddenRefresh(TransactionCase):

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
        self.env.flush_all()
        self.util = self.env["meli.util"]

    # ------------------------------------------------------------------
    def _stored(self):
        """The auth row, straight from SQL, past every ORM cache."""
        self.env.cr.execute(
            "SELECT access_token, refresh_token FROM mercadolibre_auth "
            "WHERE company_id = %s", (self.company.id,))
        return self.env.cr.fetchone()

    def _run(self, session, **kwargs):
        # The TD8 primitive opens a real second connection, which cannot see
        # this test's uncommitted auth row. AmbientAuthCursor runs its
        # statements on the test's own cursor; see that module for what it
        # fakes and what it does not claim to prove.
        self.env.flush_all()
        self.auth_cr = AmbientAuthCursor(self.env.cr)
        with patch.object(
            MeliConfiguration, "get_session", return_value=session
        ), patch.object(
            type(self.util), "_meli_auth_cursor", return_value=self.auth_cr
        ):
            client = self.util.get_new_instance(self.company, **kwargs)
        self.env.invalidate_all()
        return client

    def _assert_untouched(self, session, why):
        self.assertEqual(session.oauth_post_count, 0,
                         "%s: a token request was issued" % why)
        access, refresh = self._stored()
        self.assertEqual(access, _OLD_ACCESS, "%s: access token changed" % why)
        self.assertEqual(refresh, _OLD_REFRESH,
                         "%s: refresh token changed" % why)

    # ------------------------------------------------------------------
    # the pure constructor
    # ------------------------------------------------------------------
    def test_build_client_never_reaches_the_network(self):
        """It builds an object. That is all it may do."""
        session = _CountingSession(_Resp(200, {"id": int(_SELLER)}))
        with patch.object(MeliConfiguration, "get_session",
                          return_value=session):
            client = self.util._build_client(self.company)

        self.assertEqual(session.get_count, 0,
                         "the constructor issued a GET")
        self.assertEqual(session.oauth_post_count, 0,
                         "the constructor issued a token request")
        self.assertEqual(client.access_token, _OLD_ACCESS,
                         "the constructor did not carry the credentials over")
        self.assertEqual(client.seller_id, _SELLER)

    def test_build_client_writes_nothing(self):
        session = _CountingSession(_Resp(200, {"id": int(_SELLER)}))
        with patch.object(MeliConfiguration, "get_session",
                          return_value=session):
            self.util._build_client(self.company)
        self.env.flush_all()
        self._assert_untouched(session, "the pure constructor")

    # ------------------------------------------------------------------
    # things that must NEVER renew
    # ------------------------------------------------------------------
    def test_403_never_renews(self):
        """A 403 says nothing about the token being expired."""
        session = _CountingSession(_Resp(403, {"message": "forbidden"}))
        self._run(session)
        self._assert_untouched(session, "HTTP 403")

    def test_a_5xx_never_renews(self):
        session = _CountingSession(
            _Resp(500, {"status": 500, "cause": "Internal Server Error"}))
        self._run(session)
        self._assert_untouched(session, "HTTP 500")

    def test_a_429_never_renews(self):
        session = _CountingSession(_Resp(429, {"status": 429}))
        self._run(session)
        self._assert_untouched(session, "HTTP 429")

    def test_an_unparseable_body_never_renews(self):
        session = _CountingSession(_Resp(200, None, parseable=False))
        self._run(session)
        self._assert_untouched(session, "an unparseable body")

    def test_a_200_carrying_an_error_key_never_renews(self):
        """The old trigger. An `error` key in a 200 is not an expired token."""
        session = _CountingSession(
            _Resp(200, {"error": "something", "message": "invalid_token"}))
        self._run(session)
        self._assert_untouched(session, "a 200 body carrying an error key")

    def test_a_valid_token_never_renews(self):
        session = _CountingSession(
            _Resp(200, {"id": int(_SELLER), "nickname": "VIARENGO"}))
        self._run(session)
        self._assert_untouched(session, "a healthy session")

    # ------------------------------------------------------------------
    # the three triggers, each renewing exactly once
    # ------------------------------------------------------------------
    def test_a_401_renews_exactly_once(self):
        """A real MercadoLibre 401 carries no `error` key, and must still renew."""
        def responses(n):
            # First probe 401; after the renewal the identity check succeeds.
            if n == 1:
                return _Resp(401, {"message": "invalid or expired token"})
            return _Resp(200, {"id": int(_SELLER), "nickname": "VIARENGO"})

        session = _CountingSession(responses)
        client = self._run(session)

        self.assertEqual(session.oauth_post_count, 1,
                         "a 401 must renew exactly once")
        access, refresh = self._stored()
        self.assertEqual(access, _NEW_ACCESS)
        self.assertEqual(refresh, _NEW_REFRESH)
        self.assertEqual(client.access_token, _NEW_ACCESS,
                         "the caller did not receive the renewed credentials")

    def test_refresh_force_renews_exactly_once(self):
        session = _CountingSession(
            _Resp(200, {"id": int(_SELLER), "nickname": "VIARENGO"}))
        self._run(session, refresh_force=True)

        self.assertEqual(session.oauth_post_count, 1)
        self.assertEqual(self._stored()[1], _NEW_REFRESH)

    def test_reported_expiry_in_the_past_renews_exactly_once(self):
        """What MercadoLibre itself reported, not a guess about an error body."""
        self.company.write({
            "mercadolibre_token_expires_in": 21600,
            "mercadolibre_token_refreshed_at": "2026-01-01 00:00:00",
            "mercadolibre_token_expires_at": "2026-01-01 06:00:00",
        })
        self.env.flush_all()
        session = _CountingSession(
            _Resp(200, {"id": int(_SELLER), "nickname": "VIARENGO"}))

        self._run(session)

        self.assertEqual(session.oauth_post_count, 1,
                         "an expiry already in the past must renew")

    def test_reported_expiry_in_the_future_never_renews(self):
        self.company.write({
            "mercadolibre_token_expires_in": 21600,
            "mercadolibre_token_refreshed_at": "2099-01-01 00:00:00",
            "mercadolibre_token_expires_at": "2099-01-01 06:00:00",
        })
        self.env.flush_all()
        session = _CountingSession(
            _Resp(200, {"id": int(_SELLER), "nickname": "VIARENGO"}))

        self._run(session)
        self._assert_untouched(session, "an expiry still in the future")

    # ------------------------------------------------------------------
    # never twice
    # ------------------------------------------------------------------
    def test_a_401_after_the_renewal_does_not_renew_again(self):
        """The decisive one: one renewal per call, whatever happens next.

        A second renewal would spend the credential the first one just
        obtained, and MercadoLibre's refresh tokens are single-use.
        """
        session = _CountingSession(_Resp(401, {"message": "invalid token"}))

        self._run(session)

        self.assertEqual(
            session.oauth_post_count, 1,
            "the identity check after the renewal triggered a second renewal")

    def test_refresh_force_with_a_401_still_renews_only_once(self):
        session = _CountingSession(_Resp(401, {"message": "invalid token"}))

        self._run(session, refresh_force=True)

        self.assertEqual(session.oauth_post_count, 1)
