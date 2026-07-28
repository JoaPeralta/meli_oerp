# -*- coding: utf-8 -*-
"""Listing a seller's items must never destroy the credentials it used.

THE DEFECT
----------
``fetch_list_meli_ids`` reacted to an auth error mid-scroll by trying to blank
every credential:

    if rjson2['message']=='invalid_token' or rjson2['message']=='expired_token':
        ACCESS_TOKEN = ''
        REFRESH_TOKEN = ''
        account.write({'access_token': ACCESS_TOKEN,
                       'refresh_token': REFRESH_TOKEN, 'code': '' })
        ...
        url_login_meli = meli.auth_url()

``account`` is never assigned anywhere in the method, so the line raises
``NameError`` before it can write. The destruction is real in intent and dead by
accident -- one rename away from working. It is the same shape already removed
from ``get_fulfillment_items``: an expired **access** token says nothing about
the **refresh** token, and MercadoLibre's refresh tokens are single-use, so
blanking the row turns a routine expiry into a connector that only a manual
OAuth round can revive.

THE RETURN CONTRACT
-------------------
Every caller treats the result as a list of meli_ids:

    company.py            meli_ids = self.fetch_list_meli_ids(...)  -> len(), iterated
    product_post.py       three calls, each len() and iterated
    product.py            ids = ...fetch_list_meli_ids(...) or []   -> iterated

but the auth branches returned an ``ir.actions.act_url`` **dict**. ``len()`` of
that dict is 3, and iterating it yields the strings ``type``, ``url`` and
``target``, which the callers then treat as MercadoLibre item ids. So the old
"safe redirect" quietly fed three fake ids into the import.

The method therefore returns a list, always, and signals an unusable session by
raising ``UserError`` -- Odoo's own controlled stop: the transaction rolls back,
a human sees why, and no caller is handed something it cannot read.

On any authentication error the method must:

    not touch access_token, refresh_token or code
    not touch mercadolibre.auth at all
    not refresh, not retry, not POST to /oauth/token
    stop the operation in a controlled way

Renewal is not this method's job. The authenticated client already arrives from
``get_new_instance``.

WHAT THIS FILE DOES NOT CLAIM
-----------------------------
``filter_meli_ids`` calls ``get_new_instance()`` on every page of the scroll,
which crosses the authenticated boundary once per page. That is a separate
finding, tracked as separate debt; the tests below neutralise it with a fake so
they neither depend on it nor pin it in place.
"""

import hashlib
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_SELLER = "2288636236"
_ACCESS = "FAKE-ACCESS-000000000000-%s" % _SELLER
_REFRESH = "FAKE-REFRESH-111111111111"
_CODE = "FAKE-CODE-222222222222"


def _fp(value):
    """Fingerprint, never the value. Keeps credentials out of failure output."""
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


class _Resp:
    def __init__(self, payload, parseable=True):
        self._payload = payload
        self._parseable = parseable

    def json(self):
        if not self._parseable:
            raise ValueError("response body is not JSON")
        return self._payload


class _FakeMeli:
    """A scripted client. Never reaches a network, never renews anything."""

    def __init__(self, responses, status=None, raises=None):
        self.access_token = _ACCESS
        self.refresh_token = _REFRESH
        self.seller_id = _SELLER
        self.last_status_code = status
        self._responses = list(responses)
        self._raises = raises
        self.get_count = 0
        self.post_count = 0

    def need_login(self):
        return False

    def redirect_login(self):
        return {"type": "ir.actions.act_url", "url": "https://auth.example",
                "target": "self"}

    def auth_url(self, redirect_URI=None, state=None):
        return "https://auth.example/authorization"

    def get(self, path, params=None, **kwargs):
        self.get_count += 1
        if self._raises is not None:
            raise self._raises
        if self._responses:
            return self._responses.pop(0)
        # Deliberadamente ruidoso. Un fixture agotado significa que el metodo
        # siguio pidiendo paginas cuando ya deberia haber parado, y eso hay que
        # leerlo como "el bucle no corta", no como un cuelgue silencioso.
        raise AssertionError(
            "the method issued more GETs (%d) than the fixture scripts: the "
            "scroll loop did not stop when it should have" % self.get_count)

    def post(self, *args, **kwargs):
        self.post_count += 1
        return _Resp({})


@tagged("post_install", "-at_install")
class TestFetchListPreservesAuth(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _ACCESS,
            "mercadolibre_refresh_token": _REFRESH,
            "mercadolibre_code": _CODE,
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": "client-secret-value-4444444444",
        })
        self.env.flush_all()
        self.refresh_calls = 0

    # ------------------------------------------------------------------
    def _auth_row(self):
        """Straight from SQL, past every ORM cache. Fingerprints only."""
        self.env.cr.execute(
            "SELECT access_token, refresh_token, code FROM mercadolibre_auth "
            "WHERE company_id = %s", (self.company.id,))
        row = self.env.cr.fetchone()
        if not row:
            return None
        return {"access": _fp(row[0]), "refresh": _fp(row[1]),
                "code": _fp(row[2])}

    def _run(self, fake, params=None):
        """Drive the method with a client that cannot renew anything.

        `get_new_instance` is patched because `filter_meli_ids` calls it once
        per page; the fake keeps that off the network without this test caring
        how often it happens. `_meli_refresh_credentials` is counted rather
        than patched away, so "no renewal" is measured, not assumed.
        """
        util = type(self.env["meli.util"])
        original_refresh = util._meli_refresh_credentials

        def counting_refresh(*args, **kwargs):
            self.refresh_calls += 1
            return original_refresh(*args, **kwargs)

        with patch.object(util, "get_new_instance", return_value=fake), \
                patch.object(util, "_meli_refresh_credentials",
                             counting_refresh):
            try:
                result = self.company.fetch_list_meli_ids(params=params,
                                                          meli=fake)
                raised = None
            except Exception as exc:
                result = None
                raised = exc
        self.env.flush_all()
        self.env.invalidate_all()
        return result, raised

    def _assert_auth_untouched(self, before, fake, why):
        after = self._auth_row()
        self.assertIsNotNone(after, "%s: the auth row is gone" % why)
        self.assertEqual(
            after["refresh"], before["refresh"],
            "%s: the refresh token changed. It is single-use, so the previous "
            "one is already spent and the session cannot be renewed at all"
            % why)
        self.assertEqual(after["access"], before["access"],
                         "%s: the access token changed" % why)
        self.assertEqual(after["code"], before["code"],
                         "%s: the authorization code changed" % why)
        self.assertEqual(fake.post_count, 0,
                         "%s: a POST was issued" % why)
        self.assertEqual(self.refresh_calls, 0,
                         "%s: the method renewed the credentials. Renewal is "
                         "not its job" % why)

    def _assert_no_name_error(self, raised, why):
        self.assertNotIsInstance(
            raised, NameError,
            "%s: reached the branch that writes to an undefined `account`"
            % why)

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def _scan_then(self, error_payload, status=None):
        """A fixture that actually reaches the scroll loop.

        Three GETs happen before the loop body can hit an error: the initial
        search, whose total must exceed one page, then the response carrying a
        scroll_id, and only then the first scroll page. A shorter fixture lands
        the error before the loop starts and the test proves nothing.
        """
        return _FakeMeli([
            _Resp({"results": ["MLA1"],
                   "paging": {"total": 2, "limit": 100, "offset": 0},
                   "scroll_id": "scroll-1"}),
            _Resp(error_payload),
        ], status=status)

    def _assert_reached_the_scroll_loop(self, fake, why):
        """Positive signal, before any 'nothing changed' assertion."""
        self.assertGreaterEqual(
            fake.get_count, 2,
            "%s: the scroll loop was never reached (%d GETs), so the "
            "assertions below would hold for the wrong reason"
            % (why, fake.get_count))

    # ------------------------------------------------------------------
    # the healthy paths
    # ------------------------------------------------------------------
    def test_a_valid_first_request_returns_the_ids(self):
        fake = _FakeMeli([
            _Resp({"results": ["MLA1", "MLA2"],
                   "paging": {"total": 2, "limit": 100}}),
        ])
        before = self._auth_row()

        result, raised = self._run(fake)

        self.assertIsNone(raised)
        self.assertEqual(result, ["MLA1", "MLA2"])
        self._assert_auth_untouched(before, fake, "a healthy single page")

    def test_a_valid_multi_page_response_returns_every_id(self):
        """The scroll loop must accumulate, not stop at the first page."""
        fake = _FakeMeli([
            _Resp({"results": ["MLA1"], "scroll_id": "scroll-1",
                   "paging": {"total": 3, "limit": 1, "offset": 0}}),
            _Resp({"results": ["MLA2"], "scroll_id": "scroll-2",
                   "paging": {"total": 3, "limit": 1}}),
            _Resp({"results": ["MLA3"], "scroll_id": "",
                   "paging": {"total": 3, "limit": 1}}),
        ])
        before = self._auth_row()

        result, raised = self._run(fake)

        self.assertIsNone(raised)
        self.assertEqual(result, ["MLA1", "MLA2", "MLA3"])
        self._assert_auth_untouched(before, fake, "a healthy multi-page scan")

    def test_the_result_is_always_a_list_never_a_redirect_dict(self):
        """Callers do len() on this and iterate it.

        A dict would answer len() == 3 and iterate into the strings `type`,
        `url` and `target`, which the import then treats as item ids.
        """
        fake = _FakeMeli([
            _Resp({"results": ["MLA1"], "paging": {"total": 1, "limit": 100}}),
        ])

        result, _raised = self._run(fake)

        self.assertIsInstance(
            result, list,
            "the callers cannot read anything but a list of ids")

    # ------------------------------------------------------------------
    # the branch that used to raise NameError
    # ------------------------------------------------------------------
    def test_invalid_token_during_the_scroll_preserves_everything(self):
        fake = self._scan_then({"error": "not_found",
                                "message": "invalid_token"})
        before = self._auth_row()

        _result, raised = self._run(fake)

        self._assert_reached_the_scroll_loop(fake, "invalid_token mid-scroll")
        self._assert_no_name_error(raised, "invalid_token mid-scroll")
        self.assertIsInstance(
            raised, UserError,
            "an unusable session must stop the operation explicitly, not "
            "hand the caller a value it cannot read")
        self._assert_auth_untouched(before, fake, "invalid_token mid-scroll")

    def test_expired_token_during_the_scroll_preserves_everything(self):
        """The exact payload the destructive branch was written for."""
        fake = self._scan_then({"error": "invalid_token",
                                "message": "expired_token"})
        before = self._auth_row()

        _result, raised = self._run(fake)

        self._assert_reached_the_scroll_loop(fake, "expired_token mid-scroll")
        self._assert_no_name_error(raised, "expired_token mid-scroll")
        self.assertIsInstance(raised, UserError)
        self._assert_auth_untouched(before, fake, "expired_token mid-scroll")

    def test_a_401_during_the_scroll_preserves_everything(self):
        fake = self._scan_then({"message": "invalid or expired token"},
                               status=401)
        before = self._auth_row()

        _result, raised = self._run(fake)

        self._assert_reached_the_scroll_loop(fake, "HTTP 401 mid-scroll")
        self._assert_no_name_error(raised, "HTTP 401 mid-scroll")
        self.assertIsInstance(raised, UserError)
        self._assert_auth_untouched(before, fake, "HTTP 401 mid-scroll")

    def test_a_403_during_the_scroll_preserves_everything(self):
        fake = self._scan_then({"message": "forbidden"}, status=403)
        before = self._auth_row()

        _result, raised = self._run(fake)

        self._assert_reached_the_scroll_loop(fake, "HTTP 403 mid-scroll")
        self._assert_no_name_error(raised, "HTTP 403 mid-scroll")
        self.assertIsInstance(raised, UserError)
        self._assert_auth_untouched(before, fake, "HTTP 403 mid-scroll")

    # ------------------------------------------------------------------
    # the first request
    # ------------------------------------------------------------------
    def test_an_auth_error_on_the_first_request_preserves_everything(self):
        """The old code did not check the first response at all."""
        fake = _FakeMeli([_Resp({"error": "not_found",
                                 "message": "invalid_token"})])
        before = self._auth_row()

        _result, raised = self._run(fake)

        self.assertIsInstance(raised, UserError)
        self._assert_auth_untouched(before, fake, "invalid_token on request 1")

    def test_a_401_on_the_first_request_preserves_everything(self):
        fake = _FakeMeli([_Resp({"message": "invalid or expired token"})],
                         status=401)
        before = self._auth_row()

        _result, raised = self._run(fake)

        self.assertIsInstance(raised, UserError)
        self._assert_auth_untouched(before, fake, "HTTP 401 on request 1")

    def test_a_403_on_the_first_request_preserves_everything(self):
        fake = _FakeMeli([_Resp({"message": "forbidden"})], status=403)
        before = self._auth_row()

        _result, raised = self._run(fake)

        self.assertIsInstance(raised, UserError)
        self._assert_auth_untouched(before, fake, "HTTP 403 on request 1")

    def test_a_non_auth_error_on_the_first_request_preserves_everything(self):
        """A 500 says nothing about the credentials, so nothing is touched and
        nothing is renewed."""
        fake = _FakeMeli([_Resp({"error": "internal_error", "status": 500})],
                         status=500)
        before = self._auth_row()

        _result, raised = self._run(fake)

        self._assert_no_name_error(raised, "a 500 on request 1")
        self._assert_auth_untouched(before, fake, "a 500 on request 1")

    # ------------------------------------------------------------------
    # transport and parsing: not statements about the credentials
    # ------------------------------------------------------------------
    def test_an_unparseable_response_preserves_everything(self):
        fake = _FakeMeli([_Resp(None, parseable=False)])
        before = self._auth_row()

        _result, raised = self._run(fake)

        self._assert_no_name_error(raised, "an unparseable body")
        self._assert_auth_untouched(before, fake, "an unparseable body")

    def test_a_timeout_preserves_everything(self):
        import requests
        fake = _FakeMeli([], raises=requests.Timeout("timed out"))
        before = self._auth_row()

        _result, raised = self._run(fake)

        self._assert_no_name_error(raised, "a timeout")
        self._assert_auth_untouched(before, fake, "a timeout")

    # ------------------------------------------------------------------
    # the source may not contain the destructive write any more
    # ------------------------------------------------------------------
    def test_the_source_no_longer_writes_credentials(self):
        """Structural guard.

        The behavioural tests depend on reaching the right branch. This states
        the rule directly, so re-introducing the write fails as a statement
        about the rule rather than as a puzzle about paging.
        """
        import inspect

        from odoo.addons.meli_oerp.models import company as company_module

        source = inspect.getsource(
            company_module.res_company.fetch_list_meli_ids)
        offenders = [line.strip() for line in source.splitlines()
                     if "access_token'" in line and "write" in line
                     or "refresh_token" in line and "write" in line
                     or "account.write" in line]

        self.assertEqual(
            offenders, [],
            "fetch_list_meli_ids still writes credentials: %s" % offenders)
