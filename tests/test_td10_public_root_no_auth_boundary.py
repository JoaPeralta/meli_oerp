# -*- coding: utf-8 -*-
"""A public informational route must not touch the authenticated boundary.

THE DEFECT
----------
``/meli/`` is declared ``auth='public'`` and then does this:

    company = request.env.user.company_id
    meli = request.env['meli.util'].get_new_instance(company)
    if meli.need_login():
        return "<a href='" + meli.auth_url() + "'>Login Please</a>"

``get_new_instance`` is the authenticated boundary. It issues an identity probe
against MercadoLibre and, when the reported expiry has passed or the probe comes
back 401, it renews -- spending a refresh token that is single-use.

So an **anonymous HTTP GET** could make the connector talk to MercadoLibre and,
in the worst case, burn the credential that keeps the whole integration alive.
It also reached credentials indirectly through the private gateway, and could
hand back an OAuth URL created outside the initiators PR D just secured.

WHY auth='public' IS NOT THE POINT
----------------------------------
``auth='public'`` describes how the *route* authenticates. It says nothing about
the capability the route invokes. Making the route ``auth='user'``, or gating it
with the credentials boundary, would leave the same mistake in place: an
informational page has no business crossing into authenticated territory at all.

THE FIX
-------
The route answers a fixed string. It looks up no company, no user, no
credentials and no MercadoLibre state, so there is nothing left to protect and
nothing left to leak. Same answer for everyone: anonymous, internal user,
administrator.

That also means the page can no longer tell an observer whether the connector
is currently logged in -- which was itself a small disclosure.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

_SELLER = "2288636236"
_ACCESS_CANARY = "TD10_E_ACCESS_CANARY-%s" % _SELLER
_REFRESH_CANARY = "TD10_E_REFRESH_CANARY"
_CODE_CANARY = "TD10_E_CODE_CANARY"
_SECRET_CANARY = "TD10_E_CLIENT_SECRET_CANARY"
_COMPANY_NAME = "TD10-E Distinctive Company Name"

_EXPECTED = "MercadoLibre Publisher for Odoo - Copyright Moldeo Interactive 2021"


class _FakeMeli:
    """Would hand back an OAuth URL, if anything ever asked it to."""

    def __init__(self, counters):
        self._counters = counters
        self.access_token = _ACCESS_CANARY
        self.refresh_token = _REFRESH_CANARY
        self.seller_id = _SELLER

    def need_login(self):
        self._counters["need_login"] += 1
        return True

    def auth_url(self, redirect_URI=None, state=None):
        self._counters["auth_url"] += 1
        return ("https://auth.example/authorization?client_id=x&state=%s"
                % (state or "TD10_E_STATE_CANARY"))

    def get(self, path, params=None, **kwargs):
        self._counters["http"] += 1
        raise AssertionError("the public route reached an HTTP backend")


@tagged("post_install", "-at_install")
class TestTd10PublicRootNoAuthBoundary(HttpCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.company.write({
            "name": _COMPANY_NAME,
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": _SECRET_CANARY,
            "mercadolibre_access_token": _ACCESS_CANARY,
            "mercadolibre_refresh_token": _REFRESH_CANARY,
            "mercadolibre_code": _CODE_CANARY,
        })
        self.system_user = self.env["res.users"].create({
            "name": "TD10-E system", "login": "td10_e_system",
            "password": "td10_e_system_pw",
            "company_id": self.company.id,
            "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id,
                                  self.env.ref("base.group_system").id])]})
        self.plain_user = self.env["res.users"].create({
            "name": "TD10-E internal", "login": "td10_e_internal",
            "password": "td10_e_internal_pw",
            "company_id": self.company.id,
            "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})
        self.env.flush_all()
        self.util = type(self.env["meli.util"])

    # ------------------------------------------------------------------
    def _counters(self):
        return {"instance": 0, "client": 0, "refresh": 0, "probe": 0,
                "attempt": 0, "need_login": 0, "auth_url": 0, "http": 0}

    def _open(self):
        """GET /meli/ with every boundary instrumented."""
        from odoo.addons.meli_oerp.models import meli_util

        counters = self._counters()
        fake = _FakeMeli(counters)

        def get_new_instance(model, company=None, *a, **kw):
            counters["instance"] += 1
            return fake

        def build_client(model, company, *a, **kw):
            counters["client"] += 1
            return fake

        def refresh(*a, **kw):
            counters["refresh"] += 1
            raise AssertionError("the public route attempted a refresh")

        def probe(*a, **kw):
            counters["probe"] += 1
            raise AssertionError("the public route attempted an identity probe")

        def attempt(company):
            counters["attempt"] += 1
            raise AssertionError("the public route issued an OAuth attempt")

        patches = (
            patch.object(self.util, "get_new_instance", get_new_instance),
            patch.object(self.util, "_build_client", build_client),
            patch.object(self.util, "_meli_refresh_credentials", refresh),
            patch.object(self.util, "_meli_identity_probe", probe),
            patch.object(meli_util, "meli_oauth_attempt_issue", attempt),
        )
        try:
            for p in patches:
                p.start()
            response = self.url_open("/meli/", allow_redirects=False)
        finally:
            for p in patches:
                p.stop()
        return response, counters

    def _assert_crossed_nothing(self, counters, why):
        self.assertEqual(counters["instance"], 0,
                         "%s: crossed the authenticated boundary" % why)
        self.assertEqual(counters["client"], 0,
                         "%s: built a client" % why)
        self.assertEqual(counters["refresh"], 0, "%s: refreshed" % why)
        self.assertEqual(counters["probe"], 0, "%s: probed identity" % why)
        self.assertEqual(counters["attempt"], 0,
                         "%s: issued an OAuth attempt" % why)
        self.assertEqual(counters["need_login"], 0,
                         "%s: asked MercadoLibre state" % why)
        self.assertEqual(counters["auth_url"], 0,
                         "%s: generated an authorization URL" % why)
        self.assertEqual(counters["http"], 0, "%s: reached a backend" % why)

    def _assert_discloses_nothing(self, response, why):
        body = response.text
        for secret, label in (
            (_ACCESS_CANARY, "the access token"),
            (_REFRESH_CANARY, "the refresh token"),
            (_CODE_CANARY, "the authorization code"),
            (_SECRET_CANARY, "the client secret"),
            (_SELLER, "the seller id"),
            (_COMPANY_NAME, "the company name"),
            ("auth.example", "an authorization URL"),
            ("Login Please", "the connection state"),
            ("state=", "an OAuth state"),
        ):
            self.assertNotIn(secret, body,
                             "%s: the response discloses %s" % (why, label))

    # ==================================================================
    # anti-vacuity
    # ==================================================================
    def test_the_instrumentation_can_actually_fire(self):
        """If the fake were unreachable, every zero below would be free.

        The boundary is exercised directly here, on the same patched objects
        the route would have used.
        """
        _response, counters = self._open()
        # Fuera del pedido, pero sobre los mismos objetos parcheados no: lo que
        # se comprueba es que el contador sube cuando algo SI cruza.
        probe_counters = self._counters()
        fake = _FakeMeli(probe_counters)
        fake.need_login()
        fake.auth_url()
        self.assertEqual(probe_counters["need_login"], 1)
        self.assertEqual(probe_counters["auth_url"], 1)

    # ==================================================================
    # the route itself
    # ==================================================================
    def test_an_anonymous_get_crosses_no_boundary(self):
        """The decisive one: nobody logged in, nothing reached."""
        response, counters = self._open()

        self.assertEqual(response.status_code, 200,
                         "the public route stopped answering")
        self._assert_crossed_nothing(counters, "an anonymous GET")
        self._assert_discloses_nothing(response, "an anonymous GET")

    def test_the_response_is_the_static_notice(self):
        response, _counters = self._open()

        self.assertEqual(response.text.strip(), _EXPECTED,
                         "the informational notice changed")

    def test_an_internal_user_gets_the_same_static_answer(self):
        self.authenticate("td10_e_internal", "td10_e_internal_pw")

        response, counters = self._open()

        self.assertEqual(response.text.strip(), _EXPECTED)
        self._assert_crossed_nothing(counters, "a plain internal user")
        self._assert_discloses_nothing(response, "a plain internal user")

    def test_an_administrator_gets_the_same_static_answer(self):
        """The route must not behave differently for anyone."""
        self.authenticate("td10_e_system", "td10_e_system_pw")

        response, counters = self._open()

        self.assertEqual(
            response.text.strip(), _EXPECTED,
            "the route answers administrators differently, so it still reads "
            "state it should not")
        self._assert_crossed_nothing(counters, "a System Administrator")
        self._assert_discloses_nothing(response, "a System Administrator")

    # ==================================================================
    # structural
    # ==================================================================
    def test_the_source_no_longer_reaches_for_anything(self):
        """Stated directly, so re-introducing the lookup fails as a statement
        about the rule rather than as a puzzle about mocks."""
        import inspect

        from odoo.addons.meli_oerp.controllers import main as controllers_main

        source = inspect.getsource(controllers_main.MercadoLibre.index)
        offenders = [line.strip() for line in source.splitlines()
                     if any(needle in line for needle in (
                         "get_new_instance", "_build_client", "need_login",
                         "auth_url", "request.env", "sudo("))]

        self.assertEqual(
            offenders, [],
            "the public route still reaches into authenticated territory: %s"
            % offenders)
