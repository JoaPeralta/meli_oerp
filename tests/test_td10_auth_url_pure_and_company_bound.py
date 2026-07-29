# -*- coding: utf-8 -*-
"""Working out the OAuth domain must be local, pure, and about the right company.

THE DEFECT
----------
``meli_login`` binds the attempt to ``self`` and builds the client with the pure
constructor, and then does this:

    meli.AUTH_URL = self.get_ML_AUTH_URL(meli=meli)

``get_ML_AUTH_URL`` goes through ``_get_ML_sites``, which starts with:

    company = self.env.user.company_id
    response = meli.get("/sites", {"access_token": str(meli.access_token)})

So the attempt was pinned to company B while the **domain** could still be
worked out from company A -- the active one -- and the reconnection path made a
network call, authenticated with the very access token that may be the reason
the user is reconnecting in the first place.

WHY THE NETWORK CALL WAS NEVER NEEDED
-------------------------------------
``/sites`` only ever *adds* entries to a local map, keyed by
``default_currency_id``. The resolution then reads ``ML_sites[currency]["id"]``,
and every currency ``mercadolibre_currency`` can hold is already in that local
map. The remote answer cannot change the outcome. It is dead weight on a path
that must work precisely when the credentials do not.

WHY PR D's TESTS MISSED IT
--------------------------
Their fake answered ``get()`` with ``None`` instead of forbidding the call, so
the crossing was invisible: harmless in the test, a real request in production.
The fake here **raises** on ``get``, which is the difference between "nothing
went wrong" and "nothing was attempted".

A PURE CONSTRUCTOR IS NOT THE SAME AS A PURE PATH
-------------------------------------------------
``_build_client`` was made pure in the capability-gateway change: it touches no
network. That says nothing about what the caller does with the client
afterwards. Here the very next line reached out over HTTP. The property worth
holding is about the whole path, not one function.
"""

import hashlib
import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

_SELLER_A = "2288636236"
_SELLER_B = "9999999999"

_A_ACCESS = "TD10_F_A_ACCESS_CANARY-%s" % _SELLER_A
_A_REFRESH = "TD10_F_A_REFRESH_CANARY"
_B_ACCESS = "TD10_F_B_ACCESS_CANARY-%s" % _SELLER_B
_B_REFRESH = "TD10_F_B_REFRESH_CANARY"
_SECRET_CANARY = "TD10_F_CLIENT_SECRET_CANARY"
_OLD_CODE = "TD10_F_OLD_CODE_CANARY"

_NEW_ACCESS_B = "TD10_F_NEW_B_ACCESS_CANARY-%s" % _SELLER_B
_NEW_REFRESH_B = "TD10_F_NEW_B_REFRESH_CANARY"
_FAKE_CODE = "TD10-F-FAKE-AUTH-CODE"

_A_NAME = "TD10-F Argentina Company"
_B_NAME = "TD10-F Brasil Company"

# Argentina -> MLA -> auth.mercadolibre.com.ar
# Brasil    -> MLB -> auth.mercadolivre.com.br   (dominio claramente distinto)
_A_DOMAIN = "auth.mercadolibre.com.ar"
_B_DOMAIN = "auth.mercadolivre.com.br"


def _fp(value):
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


class _NoNetworkMeli:
    """A client that refuses to be used as a network client.

    Answering None would hide the crossing; raising makes it impossible to
    confuse "nothing went wrong" with "nothing was attempted".
    """

    def __init__(self, counters, seller, access="", refresh=""):
        self._counters = counters
        self.seller_id = seller
        self.access_token = access
        self.refresh_token = refresh
        self.client_id = "1111111111111111"
        self.client_secret = _SECRET_CANARY
        self.redirect_uri = "https://example.test/meli_login"
        self.AUTH_URL = "https://auth.mercadolibre.com.ar/authorization"

    def get(self, path, params=None, **kwargs):
        self._counters["get"] += 1
        raise AssertionError(
            "the OAuth path made a network call: GET %s" % path)

    def post(self, *a, **kw):
        self._counters["post"] += 1
        raise AssertionError("the OAuth path made a network POST")

    def need_login(self):
        return True

    def auth_url(self, redirect_URI=None, state=None):
        self._counters["auth_url"] += 1
        return "%s?client_id=%s&state=%s" % (
            self.AUTH_URL, self.client_id, state or "")

    def redirect_login(self):
        return {"type": "ir.actions.act_url", "url": str(self.auth_url()),
                "target": "self"}

    def authorize(self, code, redirect_uri=None):
        self._counters["authorize"] += 1
        return {"access_token": _NEW_ACCESS_B, "refresh_token": _NEW_REFRESH_B,
                "token_type": "Bearer", "expires_in": 21600,
                "user_id": int(_SELLER_B)}


@tagged("post_install", "-at_install")
class TestTd10AuthUrlPureAndCompanyBound(HttpCase):

    def setUp(self):
        super().setUp()
        Country = self.env["res.country"]
        self.company_a = self.env.ref("base.main_company")
        self.company_b = self.env["res.company"].create({"name": _B_NAME})

        self.company_a.write({
            "name": _A_NAME,
            "country_id": Country.search([("code", "=", "AR")], limit=1).id,
            "mercadolibre_currency": "ARS",
            "mercadolibre_seller_id": _SELLER_A,
            "mercadolibre_client_id": "1111111111111111",
            "mercadolibre_secret_key": _SECRET_CANARY,
            "mercadolibre_redirect_uri": "https://example.test/meli_login",
            "mercadolibre_access_token": _A_ACCESS,
            "mercadolibre_refresh_token": _A_REFRESH,
        })
        self.company_b.write({
            "country_id": Country.search([("code", "=", "BR")], limit=1).id,
            "mercadolibre_currency": "BRL",
            "mercadolibre_seller_id": _SELLER_B,
            "mercadolibre_client_id": "2222222222222222",
            "mercadolibre_secret_key": _SECRET_CANARY,
            "mercadolibre_redirect_uri": "https://example.test/meli_login",
            "mercadolibre_access_token": _B_ACCESS,
            "mercadolibre_refresh_token": _B_REFRESH,
            "mercadolibre_code": _OLD_CODE,
        })

        self.system_user = self.env["res.users"].create({
            "name": "TD10-F system", "login": "td10_f_system",
            "password": "td10_f_system_pw",
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id, self.company_b.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id,
                                  self.env.ref("base.group_system").id])]})
        self.env.flush_all()
        self.util = type(self.env["meli.util"])

    # ------------------------------------------------------------------
    def _counters(self):
        return {"get": 0, "post": 0, "auth_url": 0, "authorize": 0,
                "instance": 0, "probe": 0, "refresh": 0, "attempt": 0}

    def _row(self, company):
        self.env.cr.execute(
            "SELECT access_token, refresh_token FROM mercadolibre_auth "
            "WHERE company_id = %s", (company.id,))
        row = self.env.cr.fetchone()
        return {"access": _fp(row[0]), "refresh": _fp(row[1])} if row else None

    def _guarded(self, counters, fake):
        from odoo.addons.meli_oerp.models import meli_util

        original_issue = meli_util.meli_oauth_attempt_issue

        def build_client(model, company, *a, **kw):
            return fake

        def get_new_instance(model, company=None, *a, **kw):
            counters["instance"] += 1
            raise AssertionError(
                "the OAuth path crossed the authenticated boundary")

        def probe(*a, **kw):
            counters["probe"] += 1
            raise AssertionError("the OAuth path probed identity")

        def refresh(*a, **kw):
            counters["refresh"] += 1
            raise AssertionError("the OAuth path refreshed")

        def attempt(company):
            counters["attempt"] += 1
            return original_issue(company)

        return (patch.object(self.util, "_build_client", build_client),
                patch.object(self.util, "get_new_instance", get_new_instance),
                patch.object(self.util, "_meli_identity_probe", probe),
                patch.object(self.util, "_meli_refresh_credentials", refresh),
                patch.object(meli_util, "meli_oauth_attempt_issue", attempt))

    def _press_button(self, company, fake, counters):
        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            response = self.url_open(
                "/web/dataset/call_kw",
                data=json.dumps({
                    "jsonrpc": "2.0", "method": "call",
                    "params": {"model": "res.company", "method": "meli_login",
                               "args": [[company.id]], "kwargs": {}}}),
                headers={"Content-Type": "application/json"})
        finally:
            for p in patches:
                p.stop()
        payload = response.json()
        self.assertNotIn("error", payload,
                         "meli_login failed over RPC: %s"
                         % str(payload.get("error"))[:300])
        return (payload.get("result") or {}).get("url", "")

    # ==================================================================
    # positive controls: the spies can fire
    # ==================================================================
    def test_the_network_spy_fires_when_something_calls_get(self):
        counters = self._counters()
        fake = _NoNetworkMeli(counters, _SELLER_B)
        with self.assertRaises(AssertionError):
            fake.get("/sites")
        self.assertEqual(counters["get"], 1,
                         "the network spy cannot fire, so the zeros below "
                         "would be free")

    def test_the_authorize_spy_fires_when_something_exchanges(self):
        counters = self._counters()
        fake = _NoNetworkMeli(counters, _SELLER_B)
        fake.authorize("x")
        self.assertEqual(counters["authorize"], 1)

    def test_the_two_companies_resolve_to_different_domains(self):
        """Anti-vacuity: if both resolved the same, nothing below could tell
        the right company from the wrong one."""
        self.assertNotEqual(_A_DOMAIN, _B_DOMAIN)
        self.assertEqual(self.company_a.mercadolibre_currency, "ARS")
        self.assertEqual(self.company_b.mercadolibre_currency, "BRL")

    # ==================================================================
    # the button
    # ==================================================================
    def test_the_button_uses_the_domain_of_the_company_it_was_pressed_on(self):
        """The decisive one: attempt pinned to B, domain must be B's too."""
        self.authenticate("td10_f_system", "td10_f_system_pw")
        self.assertEqual(self.system_user.company_id, self.company_a,
                         "the active company is not A, so this proves nothing")
        counters = self._counters()
        fake = _NoNetworkMeli(counters, _SELLER_B, _B_ACCESS, _B_REFRESH)

        url = self._press_button(self.company_b, fake, counters)

        self.assertIn(_B_DOMAIN, url,
                      "the OAuth domain came from the active company instead "
                      "of the one the button was pressed on")
        self.assertNotIn(_A_DOMAIN, url)

    def test_the_button_touches_no_network(self):
        self.authenticate("td10_f_system", "td10_f_system_pw")
        counters = self._counters()
        fake = _NoNetworkMeli(counters, _SELLER_B, _B_ACCESS, _B_REFRESH)

        url = self._press_button(self.company_b, fake, counters)

        self.assertEqual(counters["get"], 0, "the button called MercadoLibre")
        self.assertEqual(counters["post"], 0)
        self.assertEqual(counters["instance"], 0)
        self.assertEqual(counters["probe"], 0)
        self.assertEqual(counters["refresh"], 0)
        self.assertEqual(counters["attempt"], 1,
                         "expected exactly one attempt, got %d"
                         % counters["attempt"])
        self.assertEqual(counters["auth_url"], 1,
                         "expected exactly one authorization URL")
        self.assertTrue(url)

    def test_the_button_works_with_no_usable_credentials(self):
        """Reconnecting must not depend on the credentials that failed."""
        self.company_b.write({"mercadolibre_access_token": "",
                              "mercadolibre_refresh_token": ""})
        self.env.flush_all()
        self.authenticate("td10_f_system", "td10_f_system_pw")
        counters = self._counters()
        fake = _NoNetworkMeli(counters, _SELLER_B, "", "")

        url = self._press_button(self.company_b, fake, counters)

        self.assertIn(_B_DOMAIN, url,
                      "a company with no credentials cannot start a "
                      "reconnection")
        self.assertEqual(counters["get"], 0)

    def test_the_authorization_url_carries_no_secret(self):
        self.authenticate("td10_f_system", "td10_f_system_pw")
        counters = self._counters()
        fake = _NoNetworkMeli(counters, _SELLER_B, _B_ACCESS, _B_REFRESH)

        url = self._press_button(self.company_b, fake, counters)

        for secret, label in ((_B_ACCESS, "the access token"),
                              (_B_REFRESH, "the refresh token"),
                              (_OLD_CODE, "the previous authorization code"),
                              (_SECRET_CANARY, "the client secret"),
                              (_SELLER_B, "the seller id"),
                              (_A_NAME, "the other company's name")):
            self.assertNotIn(secret, url,
                             "the authorization URL carries %s" % label)

    # ==================================================================
    # the direct entry point
    # ==================================================================
    def test_the_direct_entry_point_touches_no_network(self):
        self.authenticate("td10_f_system", "td10_f_system_pw")
        counters = self._counters()
        fake = _NoNetworkMeli(counters, _SELLER_A, _A_ACCESS, _A_REFRESH)
        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            response = self.url_open("/meli_login", allow_redirects=False)
        finally:
            for p in patches:
                p.stop()

        self.assertIn("state=", response.text,
                      "the direct entry point stopped issuing an attempt")
        self.assertEqual(counters["get"], 0,
                         "the direct entry point called MercadoLibre")
        self.assertEqual(counters["instance"], 0)
        self.assertEqual(counters["attempt"], 1)

    # ==================================================================
    # the callback
    # ==================================================================
    def test_the_callback_exchanges_once_and_calls_nothing_else(self):
        """Attempt for B, returning with A active."""
        self.authenticate("td10_f_system", "td10_f_system_pw")
        counters = self._counters()
        fake = _NoNetworkMeli(counters, _SELLER_B, _B_ACCESS, _B_REFRESH)
        before_a = self._row(self.company_a)

        url = self._press_button(self.company_b, fake, counters)
        state = url.split("state=", 1)[1].split("&")[0]

        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            response = self.url_open(
                "/meli_login?code=%s&state=%s" % (_FAKE_CODE, state),
                allow_redirects=False)
        finally:
            for p in patches:
                p.stop()
        self.env.invalidate_all()

        self.assertIn("completed successfully", response.text,
                      "the legitimate callback stopped working")
        self.assertEqual(counters["get"], 0,
                         "the callback called MercadoLibre before authorize()")
        self.assertEqual(counters["authorize"], 1,
                         "expected exactly one code exchange")
        self.assertEqual(counters["probe"], 0)
        self.assertEqual(counters["refresh"], 0)
        self.assertEqual(self._row(self.company_b)["access"],
                         _fp(_NEW_ACCESS_B),
                         "company B did not receive its credentials")
        self.assertEqual(self._row(self.company_a), before_a,
                         "company A was touched")

    # ==================================================================
    # structural: the AUTH URL resolution is pure
    # ==================================================================
    def test_resolving_the_auth_url_is_pure(self):
        """Called directly with no client at all, it must still answer."""
        url_a = self.company_a.get_ML_AUTH_URL()
        url_b = self.company_b.get_ML_AUTH_URL()

        self.assertIn(_A_DOMAIN, url_a)
        self.assertIn(_B_DOMAIN, url_b)
        self.assertNotEqual(url_a, url_b,
                            "both companies resolved to the same domain")

    def test_resolving_the_auth_url_ignores_the_active_company(self):
        """Even asked as a user sitting in A, company B answers with B."""
        company_b = self.company_b.with_user(self.system_user)

        self.assertIn(_B_DOMAIN, company_b.get_ML_AUTH_URL(),
                      "the resolution followed the active company")

    def test_resolving_the_auth_url_never_uses_the_client(self):
        """A client that raises on every call must not disturb it."""
        counters = self._counters()
        fake = _NoNetworkMeli(counters, _SELLER_B, _B_ACCESS, _B_REFRESH)

        url = self.company_b.get_ML_AUTH_URL(meli=fake)

        self.assertIn(_B_DOMAIN, url)
        self.assertEqual(counters["get"], 0,
                         "resolving the domain used the client")
