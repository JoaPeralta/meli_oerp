# -*- coding: utf-8 -*-
"""Pausing unregistered items must never destroy the credentials it used.

THE DEFECT
----------
``meli_pause_all`` reads the seller's item list in up to three stages -- the
initial search, then either scan pagination or offset pagination -- and each
stage reacted to an auth error the same way:

    if rjson['message']=='invalid_token' or rjson['message']=='expired_token':
        ACCESS_TOKEN = ''
        REFRESH_TOKEN = ''
        company.write({'mercadolibre_access_token': ACCESS_TOKEN,
                       'mercadolibre_refresh_token': REFRESH_TOKEN,
                       'mercadolibre_code': '' })

This is the same shape already removed from ``get_fulfillment_items`` and
``fetch_list_meli_ids``, with one difference that matters: there the write
targeted an ``account`` name that was never assigned, so it died of NameError
before it could do anything. **Here ``company`` is a real recordset.** These
three writes execute.

An expired **access** token says nothing about the **refresh** token, and
MercadoLibre's refresh tokens are single-use, so blanking the row turns a
routine expiry -- hit while pausing items, of all things -- into a connector
that only a manual OAuth round can revive.

The button that reaches this method asks "¿Está seguro que quiere pausar todos
sus productos de ML?". It does not mention credentials, because pausing items
has nothing to do with them.

THE CONTRACT
------------
On HTTP 401, HTTP 403, ``invalid_token`` or ``expired_token`` in any of the
three read stages:

    access_token, refresh_token and code untouched
    mercadolibre.auth untouched
    no extra refresh, no retry, no POST to /oauth/token
    the operation aborts in a controlled way, with UserError

and no automatic OAuth redirect from the middle of the operation: a redirect
hides the fact that nothing was paused.

THE PROPERTY THAT MATTERS
-------------------------
All the reading happens **before** the ``put_mini`` loop. So an auth error in
any read stage must leave ``put_mini_count == 0``: not one item is paused, and
the credentials that could not read are still the credentials on file.

NOT IN SCOPE HERE
-----------------
Partial failures inside the real pausing loop, ``filter_meli_ids``, the button
wording, and any pagination refactor. Separate findings.
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

_IN_ODOO = "MLA-ALREADY-IN-ODOO"
_NOT_IN_ODOO = "MLA-NOT-IN-ODOO"

# Las cuatro senales que significan "la sesion no sirve", y nada mas.
_INVALID = ({"error": "not_found", "message": "invalid_token"}, 200)
_EXPIRED = ({"error": "invalid_token", "message": "expired_token"}, 200)
_H401 = ({"message": "invalid or expired token"}, 401)
_H403 = ({"message": "forbidden"}, 403)


def _fp(value):
    """Fingerprint, never the value."""
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def json(self):
        return self._payload


class _FakeMeli:
    """A scripted client. No network, no renewal, and it counts every pause."""

    def __init__(self, responses):
        self.access_token = _ACCESS
        self.refresh_token = _REFRESH
        self.seller_id = _SELLER
        self.last_status_code = None
        self._responses = list(responses)
        self.get_count = 0
        self.put_mini_count = 0
        self.put_mini_ids = []
        self.post_count = 0

    def need_login(self):
        return False

    def auth_url(self, redirect_URI=None, state=None):
        return "https://auth.example/authorization"

    def get(self, path, params=None, **kwargs):
        self.get_count += 1
        if self._responses:
            response = self._responses.pop(0)
            self.last_status_code = response.status
            return response
        raise AssertionError(
            "the method issued more GETs (%d) than the fixture scripts: the "
            "pagination did not stop when it should have" % self.get_count)

    def put_mini(self, path, body=None, params=None, **kwargs):
        self.put_mini_count += 1
        self.put_mini_ids.append(path)
        return _Resp({"id": path.rsplit("/", 1)[-1], "status": "paused"})

    def post(self, *args, **kwargs):
        self.post_count += 1
        return _Resp({})


@tagged("post_install", "-at_install")
class TestPauseAllPreservesAuth(TransactionCase):

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
            "mercadolibre_process_offset": 0,
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

    def _run(self, fake):
        util = type(self.env["meli.util"])
        original_refresh = util._meli_refresh_credentials

        def counting_refresh(*args, **kwargs):
            self.refresh_calls += 1
            return original_refresh(*args, **kwargs)

        with patch.object(util, "get_new_instance", return_value=fake), \
                patch.object(util, "_meli_refresh_credentials",
                             counting_refresh):
            try:
                result = self.company.meli_pause_all()
                raised = None
            except Exception as exc:
                result = None
                raised = exc
        self.env.flush_all()
        self.env.invalidate_all()
        return result, raised

    # ------------------------------------------------------------------
    def _assert_aborted_without_touching_anything(self, before, fake, raised,
                                                  why):
        # Primero la senal positiva: que el error haya llegado como parada
        # explicita. Sin esto, todo lo de abajo se cumpliria igual si el metodo
        # simplemente no hubiera hecho nada.
        self.assertIsInstance(
            raised, UserError,
            "%s: the operation did not abort explicitly (got %r)"
            % (why, raised))
        self.assertEqual(
            fake.put_mini_count, 0,
            "%s: %d item(s) were paused despite the session being unusable"
            % (why, fake.put_mini_count))

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
        self.assertEqual(fake.post_count, 0, "%s: a POST was issued" % why)
        self.assertEqual(
            self.refresh_calls, 0,
            "%s: the method renewed the credentials on its own" % why)

    # ------------------------------------------------------------------
    # fixtures, one per read stage
    # ------------------------------------------------------------------
    def _stage_initial(self, signal):
        """The very first search fails."""
        payload, status = signal
        return _FakeMeli([_Resp(payload, status=status)])

    def _stage_scan(self, signal):
        """total > 1000 selects the scan path; the error lands in its loop."""
        payload, status = signal
        return _FakeMeli([
            _Resp({"results": [_NOT_IN_ODOO],
                   "paging": {"total": 1500, "limit": 100, "offset": 0}}),
            _Resp({"results": [_NOT_IN_ODOO], "scroll_id": "sc-1",
                   "paging": {"total": 1500, "limit": 100, "offset": 0}}),
            _Resp(payload, status=status),
        ])

    def _stage_offset(self, signal):
        """total <= 1000 but over one page selects the offset path."""
        payload, status = signal
        return _FakeMeli([
            _Resp({"results": [_NOT_IN_ODOO],
                   "paging": {"total": 300, "limit": 100, "offset": 0}}),
            _Resp(payload, status=status),
        ])

    def _assert_reached(self, fake, expected, why):
        """Positive signal before any 'nothing happened' assertion."""
        self.assertGreaterEqual(
            fake.get_count, expected,
            "%s: only %d GET(s) were issued, so the stage under test was "
            "never reached and the assertions below would hold for the wrong "
            "reason" % (why, fake.get_count))

    # ==================================================================
    # stage 1: the initial search
    # ==================================================================
    def test_invalid_token_on_the_initial_search(self):
        fake = self._stage_initial(_INVALID)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 1, "invalid_token / initial search")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "invalid_token / initial search")

    def test_expired_token_on_the_initial_search(self):
        fake = self._stage_initial(_EXPIRED)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 1, "expired_token / initial search")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "expired_token / initial search")

    def test_http_401_on_the_initial_search(self):
        fake = self._stage_initial(_H401)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 1, "HTTP 401 / initial search")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "HTTP 401 / initial search")

    def test_http_403_on_the_initial_search(self):
        fake = self._stage_initial(_H403)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 1, "HTTP 403 / initial search")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "HTTP 403 / initial search")

    # ==================================================================
    # stage 2: scan pagination
    # ==================================================================
    def test_invalid_token_during_scan_pagination(self):
        fake = self._stage_scan(_INVALID)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 3, "invalid_token / scan pagination")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "invalid_token / scan pagination")

    def test_expired_token_during_scan_pagination(self):
        fake = self._stage_scan(_EXPIRED)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 3, "expired_token / scan pagination")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "expired_token / scan pagination")

    def test_http_401_during_scan_pagination(self):
        fake = self._stage_scan(_H401)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 3, "HTTP 401 / scan pagination")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "HTTP 401 / scan pagination")

    def test_http_403_during_scan_pagination(self):
        fake = self._stage_scan(_H403)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 3, "HTTP 403 / scan pagination")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "HTTP 403 / scan pagination")

    # ==================================================================
    # stage 3: offset pagination
    # ==================================================================
    def test_invalid_token_during_offset_pagination(self):
        fake = self._stage_offset(_INVALID)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 2, "invalid_token / offset pagination")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "invalid_token / offset pagination")

    def test_expired_token_during_offset_pagination(self):
        fake = self._stage_offset(_EXPIRED)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 2, "expired_token / offset pagination")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "expired_token / offset pagination")

    def test_http_401_during_offset_pagination(self):
        fake = self._stage_offset(_H401)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 2, "HTTP 401 / offset pagination")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "HTTP 401 / offset pagination")

    def test_http_403_during_offset_pagination(self):
        fake = self._stage_offset(_H403)
        before = self._auth_row()
        _r, raised = self._run(fake)
        self._assert_reached(fake, 2, "HTTP 403 / offset pagination")
        self._assert_aborted_without_touching_anything(
            before, fake, raised, "HTTP 403 / offset pagination")

    # ==================================================================
    # the valid path: commercial behaviour must not change
    # ==================================================================
    def test_a_healthy_run_pauses_only_what_odoo_does_not_have(self):
        """The whole point of the button, and it must still work.

        An item already known to Odoo is left alone; one that is not gets
        paused. That is the existing behaviour and this PR does not touch it.
        """
        self.env["product.product"].create({
            "name": "Producto ya registrado (test)",
            "meli_id": _IN_ODOO,
        })
        self.env.flush_all()

        fake = _FakeMeli([
            _Resp({"results": [_IN_ODOO, _NOT_IN_ODOO],
                   "paging": {"total": 2, "limit": 100, "offset": 0}}),
        ])
        before = self._auth_row()

        _result, raised = self._run(fake)

        self.assertIsNone(raised, "a healthy run raised %r" % raised)
        self.assertEqual(
            fake.put_mini_count, 1,
            "expected exactly the unregistered item to be paused, got %s"
            % fake.put_mini_ids)
        self.assertEqual(fake.put_mini_ids, ["/items/" + _NOT_IN_ODOO],
                         "the wrong item was paused")
        self.assertEqual(self._auth_row(), before,
                         "a healthy run changed the credentials")
        self.assertEqual(self.refresh_calls, 0)
        self.assertEqual(fake.post_count, 0)

    # ==================================================================
    # structural
    # ==================================================================
    def test_the_source_no_longer_writes_credentials(self):
        """Stated directly, so re-introducing any of the three writes fails as
        a statement about the rule rather than as a puzzle about pagination."""
        import inspect

        from odoo.addons.meli_oerp.models import company as company_module

        source = inspect.getsource(company_module.res_company.meli_pause_all)
        offenders = [line.strip() for line in source.splitlines()
                     if "write" in line and ("access_token" in line
                                             or "refresh_token" in line
                                             or "mercadolibre_code" in line)]

        self.assertEqual(
            offenders, [],
            "meli_pause_all still writes credential fields: %s" % offenders)
