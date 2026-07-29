# -*- coding: utf-8 -*-
"""Connecting and disconnecting MercadoLibre are administrator operations.

THE DEFECT
----------
``meli_login`` and ``meli_logout`` are public methods on ``res.company``. A
public method on a model is reachable over RPC by any logged-in user -- the
button is one way in, not the only way. Neither checks anything:

    def meli_login(self):
        self.ensure_one()
        company = self.env.user.company_id      # <- self is then ignored
        meli = self.env['meli.util'].get_new_instance(company)
        return meli.redirect_login()

Two problems in four lines.

**No authorisation.** Any internal user can call either one. ``meli_logout``
destroys the stored credentials -- and MercadoLibre's refresh tokens are
single-use, so that is not recoverable without a fresh OAuth round.
``meli_login`` reaches ``get_new_instance``, the authenticated boundary, which
issues an identity probe and may spend the refresh token renewing.

**The wrong company.** ``meli_login`` calls ``ensure_one()`` on ``self`` and
then ignores it, acting on the user's *active* company instead. With more than
one company those differ, so the button on company B starts the flow for
company A. ``meli_logout`` already had this fixed; ``meli_login`` did not.

THE CONTRACT
------------
Checked inside the method, before reading a credential, building a client,
generating a URL or writing anything:

    caller       base.group_system, or the superuser
    company      taken from self, never from the active company
    singleton    self.ensure_one()
    permitted    the company must be one the user is allowed to work in --
                 being an administrator does not grant silent access to a
                 company outside that set
    denial       AccessError, and nothing else happens at all

Being denied means zero writes, zero clients built, zero requests, zero
refreshes, zero identity probes, zero POSTs to /oauth/token.

Note that "permitted" is checked against the user's *allowed* companies, not
against the currently selected ones. An administrator working with company A
active may legitimately act on company B through an explicit call; what they
may not do is act on a company they were never given.

NOT IN THIS FILE
----------------
Binding the OAuth callback to a uid and a company is PR D. The public route
that crosses the authenticated boundary is PR E. ``product_meli_login`` on
``product.product`` is the same class of defect on a different model and is
recorded as separate debt.
"""

import hashlib
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_SELLER_A = "2288636236"
_SELLER_B = "9999999999"

# Canarios DISTINTOS por compania: si fueran iguales, un test que confunde A
# con B pasaria igual y no probaria nada.
_A_ACCESS = "TD10_C_A_ACCESS_CANARY"
_A_REFRESH = "TD10_C_A_REFRESH_CANARY"
_A_CODE = "TD10_C_A_CODE_CANARY"
_B_ACCESS = "TD10_C_B_ACCESS_CANARY"
_B_REFRESH = "TD10_C_B_REFRESH_CANARY"
_B_CODE = "TD10_C_B_CODE_CANARY"

_UNAUTHORISED = ("portal", "internal", "meli_reader", "meli_manager",
                 "sales_manager")


def _fp(value):
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


class _FakeMeli:
    """Marked with the company it was built for, so a test can tell A from B."""

    def __init__(self, marker):
        self.marker = marker

    def need_login(self):
        return True

    def redirect_login(self):
        return {"type": "ir.actions.act_url",
                "url": "https://auth.example/authorization?company=%s"
                       % self.marker,
                "target": "self"}


@tagged("post_install", "-at_install")
class TestTd10RpcAuthorization(TransactionCase):

    def setUp(self):
        super().setUp()
        Company = self.env["res.company"]
        self.company_a = self.env.ref("base.main_company")
        self.company_b = Company.create({"name": "TD10-C Company B"})

        self.company_a.write({
            "mercadolibre_seller_id": _SELLER_A,
            "mercadolibre_client_id": "1111111111111111",
            "mercadolibre_secret_key": "TD10_C_A_SECRET_CANARY",
            "mercadolibre_access_token": _A_ACCESS,
            "mercadolibre_refresh_token": _A_REFRESH,
            "mercadolibre_code": _A_CODE,
        })
        self.company_b.write({
            "mercadolibre_seller_id": _SELLER_B,
            "mercadolibre_client_id": "2222222222222222",
            "mercadolibre_secret_key": "TD10_C_B_SECRET_CANARY",
            "mercadolibre_access_token": _B_ACCESS,
            "mercadolibre_refresh_token": _B_REFRESH,
            "mercadolibre_code": _B_CODE,
        })
        self.env.flush_all()

        self.actors = {}
        for key, xmlids, companies in (
            ("portal", ["base.group_portal"], [self.company_a]),
            ("internal", ["base.group_user"], [self.company_a]),
            ("meli_reader", ["base.group_user",
                             "meli_oerp.group_mercadolibre_reader"],
             [self.company_a]),
            ("meli_manager", ["base.group_user",
                              "meli_oerp.group_mercadolibre_manager"],
             [self.company_a]),
            ("sales_manager", ["base.group_user",
                               "sales_team.group_sale_manager"],
             [self.company_a]),
            # Administrador de AMBAS companias.
            ("system", ["base.group_user", "base.group_system"],
             [self.company_a, self.company_b]),
            # Administrador de A UNICAMENTE: el caso que prueba que ser admin
            # no alcanza para tocar una compania que no le dieron.
            ("system_a_only", ["base.group_user", "base.group_system"],
             [self.company_a]),
        ):
            groups = self.env["res.groups"]
            ok = True
            for xmlid in xmlids:
                rec = self.env.ref(xmlid, raise_if_not_found=False)
                if not rec:
                    ok = False
                groups |= rec or groups
            if not ok:
                continue
            self.actors[key] = self.env["res.users"].create({
                "name": "TD10-C %s" % key,
                "login": "td10_c_%s" % key,
                "company_id": companies[0].id,
                "company_ids": [(6, 0, [c.id for c in companies])],
                "group_ids": [(6, 0, groups.ids)],
            })

    # ------------------------------------------------------------------
    def _row(self, company):
        self.env.cr.execute(
            "SELECT access_token, refresh_token, code FROM mercadolibre_auth "
            "WHERE company_id = %s", (company.id,))
        row = self.env.cr.fetchone()
        if not row:
            return None
        return {"access": _fp(row[0]), "refresh": _fp(row[1]),
                "code": _fp(row[2])}

    def _spies(self):
        """Every door the methods could open. All must stay shut on denial."""
        util = type(self.env["meli.util"])
        counters = {"client": 0, "instance": 0, "refresh": 0, "probe": 0}

        def build_client(self_model, company, *a, **kw):
            counters["client"] += 1
            return _FakeMeli(company.name)

        def auth_url_of(company_record, meli=None):
            return "https://auth.example/authorization"

        def get_new_instance(self_model, company=None, *a, **kw):
            counters["instance"] += 1
            return _FakeMeli(company.name if company else "?")

        def refresh(*a, **kw):
            counters["refresh"] += 1
            raise AssertionError("a refresh was attempted")

        def probe(*a, **kw):
            counters["probe"] += 1
            raise AssertionError("an identity probe was attempted")

        return counters, (
            patch.object(util, "_build_client", build_client),
            patch.object(type(self.company_a), "get_ML_AUTH_URL", auth_url_of),
            patch.object(util, "get_new_instance", get_new_instance),
            patch.object(util, "_meli_refresh_credentials", refresh),
            patch.object(util, "_meli_identity_probe", probe),
        )

    def _fake_request(self, uid):
        """meli_login crea el intento OAuth en la sesion, asi que necesita una.

        Sin peticion HTTP falla explicitamente a proposito -eso lo cubre PR D-,
        pero aca lo que se prueba es la autorizacion, no ese contrato: se le da
        una sesion para que el rechazo, cuando llega, sea por el motivo bajo
        prueba y no por la falta de request.
        """
        class _Req:
            pass

        req = _Req()
        req.session = {}
        req.env = self.env(user=uid)
        return req

    def _call(self, actor, method, company):
        counters, patches = self._spies()
        record = company.with_user(self.actors[actor])
        patches = patches + (
            patch("odoo.http.request", self._fake_request(
                self.actors[actor].id)),)
        try:
            for p in patches:
                p.start()
            try:
                result = getattr(record, method)()
                raised = None
            except Exception as exc:
                result = None
                raised = exc
        finally:
            for p in patches:
                p.stop()
        self.env.flush_all()
        self.env.invalidate_all()
        return result, raised, counters

    def _assert_denied_with_no_effect(self, actor, method, company, why):
        before = self._row(company)
        self.assertIsNotNone(
            before, "%s: there is no auth row to protect, so this test would "
            "prove nothing" % why)

        _result, raised, counters = self._call(actor, method, company)

        self.assertIsInstance(
            raised, AccessError,
            "%s: expected AccessError, got %r" % (why, raised))
        self.assertEqual(self._row(company), before,
                         "%s: the credentials changed" % why)
        self.assertEqual(counters["client"], 0,
                         "%s: a client was built before the refusal" % why)
        self.assertEqual(counters["instance"], 0,
                         "%s: the authenticated boundary was crossed" % why)
        self.assertEqual(counters["refresh"], 0, "%s: a refresh ran" % why)
        self.assertEqual(counters["probe"], 0,
                         "%s: an identity probe ran" % why)

    # ==================================================================
    # anti-vacuity
    # ==================================================================
    def test_the_canaries_differ_between_companies(self):
        a, b = self._row(self.company_a), self._row(self.company_b)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertNotEqual(
            a["access"], b["access"],
            "both companies hold the same value, so a test that confuses one "
            "for the other would pass")

    def test_the_spies_would_fire_on_a_legitimate_call(self):
        """If the capability mock is never reachable, every 'zero calls'
        assertion above is vacuous."""
        _result, raised, counters = self._call("system", "meli_login",
                                               self.company_a)
        self.assertIsNone(raised, "the positive control raised %r" % raised)
        self.assertEqual(
            counters["client"], 1,
            "the capability boundary was never reached even when allowed, so "
            "'zero calls' proves nothing")

    # ==================================================================
    # denial
    # ==================================================================
    def test_unauthorised_actors_cannot_log_in(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            self._assert_denied_with_no_effect(
                actor, "meli_login", self.company_a,
                "%s calling meli_login over RPC" % actor)

    def test_unauthorised_actors_cannot_log_out(self):
        for actor in _UNAUTHORISED:
            if actor not in self.actors:
                continue
            self._assert_denied_with_no_effect(
                actor, "meli_logout", self.company_a,
                "%s calling meli_logout over RPC" % actor)

    # ==================================================================
    # positive controls
    # ==================================================================
    def test_a_system_administrator_can_log_in(self):
        result, raised, counters = self._call("system", "meli_login",
                                              self.company_a)
        self.assertIsNone(raised)
        self.assertEqual(result.get("type"), "ir.actions.act_url",
                         "the legitimate return contract changed")
        self.assertEqual(counters["client"], 1)

    def test_a_system_administrator_can_log_out(self):
        _result, raised, _counters = self._call("system", "meli_logout",
                                                self.company_a)
        self.assertIsNone(raised)
        after = self._row(self.company_a)
        self.assertIsNone(after["access"], "the disconnect did not run")
        self.assertIsNone(after["refresh"])

    def test_the_superuser_can_still_operate(self):
        result = self.company_a.sudo().meli_logout()
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(result.get("type"), "ir.actions.act_url")
        self.assertIsNone(self._row(self.company_a)["access"])

    # ==================================================================
    # multi-company
    # ==================================================================
    def test_logout_clears_only_the_company_it_was_called_on(self):
        before_a = self._row(self.company_a)

        self._call("system", "meli_logout", self.company_b)

        self.assertIsNone(self._row(self.company_b)["access"],
                          "company B was not disconnected")
        self.assertEqual(self._row(self.company_a), before_a,
                         "disconnecting B also cleared A")

    def test_login_uses_the_company_it_was_called_on(self):
        """The user's active company is A. The call is on B."""
        self.assertEqual(self.actors["system"].company_id, self.company_a,
                         "the active company is not A, so this proves nothing")

        result, raised, _counters = self._call("system", "meli_login",
                                               self.company_b)

        self.assertIsNone(raised)
        self.assertIn(
            self.company_b.name, result.get("url", ""),
            "meli_login started the flow for the active company instead of "
            "the one it was called on")

    def test_a_company_outside_the_users_allowed_set_is_refused(self):
        """Being an administrator is not a licence over every company."""
        if "system_a_only" not in self.actors:
            self.skipTest("actor not available")
        self.assertNotIn(
            self.company_b, self.actors["system_a_only"].company_ids,
            "the actor can reach B after all, so this proves nothing")

        self._assert_denied_with_no_effect(
            "system_a_only", "meli_logout", self.company_b,
            "an administrator of A only, acting on B")

    def test_a_multi_company_recordset_is_refused(self):
        both = self.company_a | self.company_b
        with self.assertRaises(Exception):
            both.with_user(self.actors["system"]).meli_logout()
        with self.assertRaises(Exception):
            both.with_user(self.actors["system"]).meli_login()

    # ==================================================================
    # the refusal must not describe what it protects
    # ==================================================================
    def test_the_refusal_leaks_nothing(self):
        _result, raised, _counters = self._call("internal", "meli_logout",
                                                self.company_a)
        message = str(raised)
        for canary in (_A_ACCESS, _A_REFRESH, _A_CODE,
                       "TD10_C_A_SECRET_CANARY"):
            self.assertNotIn(canary, message,
                             "the error message carries a credential")
