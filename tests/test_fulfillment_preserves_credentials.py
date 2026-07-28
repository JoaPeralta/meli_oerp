# -*- coding: utf-8 -*-
"""A read path must never destroy credentials because a request failed.

THE DEFECT
----------
``get_fulfillment_items`` reacts to an auth error by blanking both tokens:

    if rjson2['message'] in ('invalid_token', 'expired_token'):
        ACCESS_TOKEN = ''
        REFRESH_TOKEN = ''
        company.write({'mercadolibre_access_token': ACCESS_TOKEN,
                       'mercadolibre_refresh_token': REFRESH_TOKEN,
                       'mercadolibre_code': ''})

An expired **access** token does not mean the **refresh** token is dead. That is
precisely the recoverable case, and the one the whole token lifecycle exists to
handle. Erasing the refresh token turns a routine expiry into a session that
cannot be renewed at all: MercadoLibre's refresh tokens are single-use, so once
this row is blanked the previous one is gone and only a manual OAuth round
brings the connector back.

``get_new_instance`` documents the opposite rule in as many words -- "NO se
borran mercadolibre_access_token/refresh_token/code ... el refresh_token de ML
sigue siendo valido aunque el access_token haya vencido". This path contradicted
it.

There is also a plain bug in the second branch: it returns ``url_login_meli``,
which is only ever assigned inside the *first* branch. Reaching it raises
``UnboundLocalError`` on top of the data loss.

WHY IT IS TESTED ANYWAY
-----------------------
The method has no callers today -- searched across ``.py`` and ``.xml``, the only
occurrence is its own ``def``. It is not deleted on that basis: absence of a
static caller is not proof that nothing reaches it, and a landmine that destroys
credentials should be defused rather than left for whoever wires it up next.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_SELLER = "2288636236"
_ACCESS = "ACCESS-value-000000000000-%s" % _SELLER
_REFRESH = "REFRESH-value-111111111111"


class _Resp:
    def __init__(self, payload, parseable=True):
        self._payload = payload
        self._parseable = parseable

    def json(self):
        if not self._parseable:
            raise ValueError("no json")
        return self._payload


class _FakeMeli:
    """A client whose GETs are scripted. Never reaches a network."""

    def __init__(self, responses, need_login=False, raises=None):
        self.access_token = _ACCESS
        self.refresh_token = _REFRESH
        self.seller_id = _SELLER
        self._responses = list(responses)
        self._need_login = need_login
        self._raises = raises
        self.get_count = 0

    def need_login(self):
        return self._need_login

    def redirect_login(self):
        return {"type": "ir.actions.act_url", "url": "https://auth.example",
                "target": "self"}

    def auth_url(self, redirect_URI=None):
        return "https://auth.example/authorization"

    def get(self, path, params=None, **kwargs):
        self.get_count += 1
        if self._raises is not None:
            raise self._raises
        if self._responses:
            return self._responses.pop(0)
        return _Resp({"results": [], "paging": {"total": 0, "limit": 100}})


@tagged("post_install", "-at_install")
class TestFulfillmentPreservesCredentials(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _ACCESS,
            "mercadolibre_refresh_token": _REFRESH,
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": "client-secret-value-4444444444",
        })
        self.env.flush_all()

    # ------------------------------------------------------------------
    def _stored(self):
        self.env.cr.execute(
            "SELECT access_token, refresh_token FROM mercadolibre_auth "
            "WHERE company_id = %s", (self.company.id,))
        row = self.env.cr.fetchone()
        return row if row else (None, None)

    def _run(self, fake):
        with patch.object(type(self.env["meli.util"]), "get_new_instance",
                          return_value=fake):
            try:
                result = self.company.get_fulfillment_items()
                raised = None
            except Exception as exc:
                result = None
                raised = exc
        self.env.flush_all()
        self.env.invalidate_all()
        return result, raised

    def _assert_credentials_survived(self, why, raised=None):
        access, refresh = self._stored()
        self.assertEqual(
            refresh, _REFRESH,
            "%s: the refresh token was erased. It is single-use, so the "
            "previous one is already spent and the session cannot be renewed "
            "at all" % why)
        self.assertEqual(access, _ACCESS,
                         "%s: the access token was erased" % why)
        self.assertIsNotNone(access, "%s: the auth row is gone" % why)
        if raised is not None:
            self.assertNotIsInstance(
                raised, UnboundLocalError,
                "%s: reached the branch that returns an unassigned "
                "url_login_meli" % why)

    # ------------------------------------------------------------------
    # the healthy path still works
    # ------------------------------------------------------------------
    def test_a_valid_access_token_returns_results(self):
        fake = _FakeMeli([
            _Resp({"results": ["MLA1"], "paging": {"total": 1, "limit": 100}}),
        ])

        result, raised = self._run(fake)

        self.assertIsNone(raised)
        self._assert_credentials_survived("a healthy run")

    # ------------------------------------------------------------------
    # every error shape: credentials survive
    # ------------------------------------------------------------------
    def test_an_expired_token_mid_scan_preserves_the_credentials(self):
        """The exact case that erased them: `expired_token` on a later page."""
        fake = _FakeMeli([
            _Resp({"results": ["MLA1"],
                   "paging": {"total": 300, "limit": 100, "offset": 0},
                   "scroll_id": "s1"}),
            _Resp({"error": "invalid_token", "message": "expired_token"}),
        ])

        _result, raised = self._run(fake)

        self._assert_credentials_survived("an expired token mid-scan", raised)

    def test_an_invalid_token_mid_scan_preserves_the_credentials(self):
        fake = _FakeMeli([
            _Resp({"results": ["MLA1"],
                   "paging": {"total": 300, "limit": 100, "offset": 0},
                   "scroll_id": "s1"}),
            _Resp({"error": "not_found", "message": "invalid_token"}),
        ])

        _result, raised = self._run(fake)

        self._assert_credentials_survived("an invalid token mid-scan", raised)

    def test_a_401_shaped_error_preserves_the_credentials(self):
        fake = _FakeMeli([
            _Resp({"error": "unauthorized", "message": "invalid_token",
                   "status": 401}),
        ])

        _result, raised = self._run(fake)

        self._assert_credentials_survived("a 401-shaped error", raised)

    def test_a_403_shaped_error_preserves_the_credentials(self):
        fake = _FakeMeli([
            _Resp({"error": "forbidden", "message": "forbidden",
                   "status": 403}),
        ])

        _result, raised = self._run(fake)

        self._assert_credentials_survived("a 403-shaped error", raised)

    def test_a_transport_failure_preserves_the_credentials(self):
        import requests
        fake = _FakeMeli([], raises=requests.Timeout("timed out"))

        _result, raised = self._run(fake)

        self._assert_credentials_survived("a timeout", raised)

    def test_an_unparseable_response_preserves_the_credentials(self):
        fake = _FakeMeli([_Resp(None, parseable=False)])

        _result, raised = self._run(fake)

        self._assert_credentials_survived("an unparseable response", raised)

    def test_a_local_exception_preserves_the_credentials(self):
        fake = _FakeMeli([], raises=RuntimeError("something local broke"))

        _result, raised = self._run(fake)

        self._assert_credentials_survived("a local exception", raised)

    def test_need_login_returns_without_touching_the_credentials(self):
        fake = _FakeMeli([], need_login=True)

        _result, raised = self._run(fake)

        self.assertIsNone(raised)
        self._assert_credentials_survived("a client that needs login")

    # ------------------------------------------------------------------
    # the method itself may not contain the destructive write any more
    # ------------------------------------------------------------------
    def test_the_source_no_longer_blanks_credentials(self):
        """Structural guard.

        The behavioural tests above depend on reaching the right branch. This
        states the rule directly, so re-introducing the write fails as a
        statement about the rule rather than as a puzzle about paging.
        """
        import inspect

        from odoo.addons.meli_oerp.models import company as company_module

        source = inspect.getsource(
            company_module.res_company.get_fulfillment_items)
        offenders = [line.strip() for line in source.splitlines()
                     if "mercadolibre_refresh_token" in line
                     or "mercadolibre_access_token" in line]

        self.assertEqual(
            offenders, [],
            "get_fulfillment_items still writes credential fields: %s"
            % offenders)
