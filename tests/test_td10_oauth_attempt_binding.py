# -*- coding: utf-8 -*-
"""The OAuth attempt must be bound to a session, a user and a company.

THE DEFECT
----------
Two problems, one root.

**The login button created no attempt at all.** ``meli_login`` went through
``get_new_instance`` to ``redirect_login`` to ``auth_url()`` with no state, so
the URL carried ``str(datetime.now())`` and the session stayed empty. When
MercadoLibre redirected back, the callback popped nothing and refused: *"could
not be matched to an authorization request from this session"*. The button flow
could not complete OAuth at all.

Only the bare ``/meli_login`` entry point issued a real attempt, which is why
the earlier tests -- which drive that entry point directly -- never noticed.

**The callback chose the company from the active context.** It read
``request.env.user.company_id`` on the way back, so starting the flow for
company B and returning with company A active wrote B's credentials onto A.

THE CONTRACT
------------
The attempt is created where the flow starts, and carries:

    value       a random opaque nonce -- the only part MercadoLibre ever sees
    issued_at   for the TTL
    uid         who asked
    company_id  which account the credentials are for

``uid`` and ``company_id`` stay server-side in the session. The browser gets no
vote on the destination: not a query parameter, not the active company, not the
context.

The callback resolves the company from the validated attempt and nothing else,
re-checks the user may still use it, and consumes the attempt exactly once. A
replay, an altered nonce, another session or another user all fail before a
client is built or a code is exchanged.

``meli_login`` uses the pure constructor, not ``get_new_instance``. The button
exists for when the credentials no longer work, so crossing the authenticated
boundary just to produce a login URL would spend the single-use refresh token
on the way to reconnecting.

CANARIES
--------
Every value here is obviously fake, and the two companies hold different ones.
"""

import hashlib
import json
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

_SELLER_A = "2288636236"
_SELLER_B = "9999999999"

_A_ACCESS = "TD10_D_A_ACCESS_CANARY-%s" % _SELLER_A
_A_REFRESH = "TD10_D_A_REFRESH_CANARY"
_B_ACCESS = "TD10_D_B_ACCESS_CANARY-%s" % _SELLER_B
_B_REFRESH = "TD10_D_B_REFRESH_CANARY"

_NEW_ACCESS_B = "TD10_D_NEW_B_ACCESS_CANARY-%s" % _SELLER_B
_NEW_REFRESH_B = "TD10_D_NEW_B_REFRESH_CANARY"

_FAKE_CODE = "TD10-D-FAKE-AUTH-CODE"


def _fp(value):
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


class _FakeMeli:
    """Scripted client. Never reaches a network, never renews anything."""

    def __init__(self, seller, payload=None):
        self.seller_id = seller
        self.client_id = "1111111111111111"
        self.redirect_uri = "https://example.test/meli_login"
        self.AUTH_URL = "https://auth.example/authorization"
        self.access_token = ""
        self.refresh_token = ""
        self.authorize_calls = 0
        self._payload = payload

    def need_login(self):
        return True

    def auth_url(self, redirect_URI=None, state=None):
        return "https://auth.example/authorization?state=%s" % (state or "")

    def redirect_login(self):
        return {"type": "ir.actions.act_url", "url": str(self.auth_url()),
                "target": "self"}

    def authorize(self, code, redirect_uri=None):
        self.authorize_calls += 1
        return self._payload or {
            "access_token": _NEW_ACCESS_B, "refresh_token": _NEW_REFRESH_B,
            "token_type": "Bearer", "expires_in": 21600,
            "user_id": int(self.seller_id)}

    def get(self, path, params=None, **kwargs):
        # Resolver AUTH_URL sondea /sites; no contestar nada lo mantiene fuera
        # de la red.
        return None


class _NoUidEnv(object):
    """Un entorno real, pero sin uid resoluble.

    Delega todo en el env verdadero -las traducciones lo necesitan- y sólo
    responde None a `uid`. Romper el env entero probaría otra cosa: que el
    request está roto, no que la identidad no se puede resolver.
    """

    uid = None

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __call__(self, *args, **kwargs):
        return self._real(*args, **kwargs)

    def __getitem__(self, name):
        return self._real[name]


@tagged("post_install", "-at_install")
class TestTd10OauthAttemptBinding(HttpCase):

    def setUp(self):
        super().setUp()
        Company = self.env["res.company"]
        self.company_a = self.env.ref("base.main_company")
        self.company_b = Company.create({"name": "TD10-D Company B"})

        for company, seller, access, refresh in (
            (self.company_a, _SELLER_A, _A_ACCESS, _A_REFRESH),
            (self.company_b, _SELLER_B, _B_ACCESS, _B_REFRESH),
        ):
            company.write({
                "mercadolibre_seller_id": seller,
                "mercadolibre_client_id": "1111111111111111",
                "mercadolibre_secret_key": "TD10_D_SECRET_CANARY",
                "mercadolibre_redirect_uri": "https://example.test/meli_login",
                "mercadolibre_access_token": access,
                "mercadolibre_refresh_token": refresh,
            })

        # El administrador puede trabajar en ambas, con A activa.
        self.admin = self.env.ref("base.user_admin")
        self.admin.write({"company_ids": [(4, self.company_b.id)],
                          "company_id": self.company_a.id})
        # Usuarios con contrasena: authenticate() los necesita para abrir una
        # sesion HTTP real.
        self.system_user = self.env["res.users"].create({
            "name": "TD10-D system", "login": "td10_d_system",
            "password": "td10_d_system_pw",
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id, self.company_b.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id,
                                  self.env.ref("base.group_system").id])]})
        self.plain_user = self.env["res.users"].create({
            "name": "TD10-D internal", "login": "td10_d_internal",
            "password": "td10_d_internal_pw",
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})
        self.env.flush_all()
        self.util = type(self.env["meli.util"])

    # ------------------------------------------------------------------
    def _row(self, company):
        self.env.cr.execute(
            "SELECT access_token, refresh_token FROM mercadolibre_auth "
            "WHERE company_id = %s", (company.id,))
        row = self.env.cr.fetchone()
        return {"access": _fp(row[0]), "refresh": _fp(row[1])} if row else None

    def _counters(self):
        return {"client": 0, "instance": 0, "refresh": 0, "probe": 0}

    def _guarded(self, counters, fake):
        """Patches counting every boundary the flow could cross."""
        def build_client(model, company, *a, **kw):
            counters["client"] += 1
            return fake

        def get_new_instance(model, company=None, *a, **kw):
            counters["instance"] += 1
            raise AssertionError(
                "the authenticated boundary was crossed; it can refresh")

        def refresh(*a, **kw):
            counters["refresh"] += 1
            raise AssertionError("a refresh was attempted")

        def probe(*a, **kw):
            counters["probe"] += 1
            raise AssertionError("an identity probe was attempted")

        return (patch.object(self.util, "_build_client", build_client),
                patch.object(self.util, "get_new_instance", get_new_instance),
                patch.object(self.util, "_meli_refresh_credentials", refresh),
                patch.object(self.util, "_meli_identity_probe", probe))

    def _press_button(self, company, fake, counters):
        """Aprieta el boton por la MISMA superficie RPC que usa el cliente web.

        Llamar meli_login() directo desde el test no sirve: crea el intento en
        la sesion HTTP, y una llamada fuera de una peticion no tiene ninguna.
        Ademas asi el intento queda en la sesion que despues usa el callback,
        que es exactamente lo que hay que probar.
        """
        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            response = self.url_open(
                "/web/dataset/call_kw",
                data=json.dumps({
                    "jsonrpc": "2.0", "method": "call",
                    "params": {"model": "res.company",
                               "method": "meli_login",
                               "args": [[company.id]], "kwargs": {}}}),
                headers={"Content-Type": "application/json"})
        finally:
            for p in patches:
                p.stop()
        payload = response.json()
        self.assertNotIn("error", payload,
                         "meli_login failed over RPC: %s"
                         % str(payload.get("error"))[:200])
        url = (payload.get("result") or {}).get("url", "")
        self.assertIn("state=", url, "the login action carries no state")
        return url.split("state=", 1)[1].split("&")[0]

    def _callback(self, state, fake, counters, extra=""):
        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            response = self.url_open(
                "/meli_login?code=%s&state=%s%s" % (_FAKE_CODE, state, extra),
                allow_redirects=False)
        finally:
            for p in patches:
                p.stop()
        self.env.invalidate_all()
        return response

    def _fake_request(self, uid, session=None):
        class _Req:
            pass

        req = _Req()
        req.session = {} if session is None else session
        req.env = self.env(user=uid)
        return req

    # ==================================================================
    # the flow works end to end -- and is the positive control
    # ==================================================================
    def test_the_button_creates_an_attempt_the_callback_can_match(self):
        """The original evidence probe: the button flow must complete."""
        self.authenticate("admin", "admin")
        fake = _FakeMeli(_SELLER_B)
        counters = self._counters()

        state = self._press_button(self.company_b, fake, counters)
        response = self._callback(state, fake, counters)

        self.assertIn(
            "completed successfully", response.text,
            "the callback refused the state the button itself produced")
        self.assertEqual(fake.authorize_calls, 1,
                         "expected exactly one code exchange")
        self.assertEqual(counters["instance"], 0)
        self.assertEqual(counters["refresh"], 0)
        self.assertEqual(counters["probe"], 0)

    def test_the_credentials_land_on_the_company_the_button_was_pressed_on(self):
        """The decisive one: start for B, come back with A active."""
        self.authenticate("admin", "admin")
        self.assertEqual(self.admin.company_id, self.company_a,
                         "the active company is not A, so this proves nothing")
        before_a = self._row(self.company_a)
        fake = _FakeMeli(_SELLER_B)
        counters = self._counters()

        state = self._press_button(self.company_b, fake, counters)
        self._callback(state, fake, counters)

        self.assertEqual(self._row(self.company_b)["access"],
                         _fp(_NEW_ACCESS_B),
                         "company B did not receive its credentials")
        self.assertEqual(self._row(self.company_a), before_a,
                         "company A was overwritten by an attempt started for "
                         "company B")

    def test_a_company_id_in_the_query_cannot_redirect_the_credentials(self):
        """The browser does not get a vote on the destination."""
        self.authenticate("admin", "admin")
        before_a = self._row(self.company_a)
        fake = _FakeMeli(_SELLER_B)
        counters = self._counters()

        state = self._press_button(self.company_b, fake, counters)
        self._callback(state, fake, counters,
                       extra="&company_id=%s" % self.company_a.id)

        self.assertEqual(self._row(self.company_a), before_a,
                         "a company_id in the query string chose the target")
        self.assertEqual(self._row(self.company_b)["access"],
                         _fp(_NEW_ACCESS_B))

    # ==================================================================
    # single use
    # ==================================================================
    def test_the_attempt_cannot_be_replayed(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli(_SELLER_B)
        counters = self._counters()

        state = self._press_button(self.company_b, fake, counters)
        first = self._callback(state, fake, counters)
        self.assertIn("completed successfully", first.text,
                      "the first exchange did not happen")

        second = self._callback(state, fake, counters)

        self.assertNotIn("completed successfully", second.text,
                         "the same attempt was accepted twice")
        self.assertEqual(fake.authorize_calls, 1,
                         "a replay exchanged the code a second time")

    def test_an_unknown_state_is_refused_without_building_a_client(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli(_SELLER_B)
        counters = self._counters()
        before = self._row(self.company_b)

        self._press_button(self.company_b, fake, counters)
        counters["client"] = 0
        response = self._callback("not-the-issued-nonce", fake, counters)

        self.assertNotIn("completed successfully", response.text)
        self.assertEqual(fake.authorize_calls, 0, "a code was exchanged")
        self.assertEqual(counters["client"], 0,
                         "a client was built before the refusal")
        self.assertEqual(self._row(self.company_b), before)

    def test_an_attempt_from_another_session_is_refused(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli(_SELLER_B)
        counters = self._counters()
        state = self._press_button(self.company_b, fake, counters)
        before = self._row(self.company_b)

        # Sesion nueva: el intento quedo en la anterior.
        self.authenticate("admin", "admin")
        counters["client"] = 0
        response = self._callback(state, fake, counters)

        self.assertNotIn("completed successfully", response.text)
        self.assertEqual(fake.authorize_calls, 0)
        self.assertEqual(counters["client"], 0)
        self.assertEqual(self._row(self.company_b), before)

    # ==================================================================
    # the primitive itself: uid, TTL, and no session at all
    # ==================================================================
    def test_the_attempt_records_the_user_and_the_company(self):
        from odoo.addons.meli_oerp.models import meli_util

        req = self._fake_request(self.admin.id)
        with patch("odoo.http.request", req):
            meli_util.meli_oauth_attempt_issue(self.company_b)

        stored = req.session["meli_oauth_state"]
        self.assertEqual(stored["company_id"], self.company_b.id)
        self.assertEqual(stored["uid"], self.admin.id)
        self.assertNotIn(_B_ACCESS, str(stored),
                         "a credential was stored in the session")
        self.assertGreaterEqual(len(stored["value"]), 20,
                                "the nonce is too short to be unguessable")

    def test_another_user_cannot_consume_the_attempt(self):
        """Same session, different user.

        Isolates the uid check from the session check, which would otherwise
        hide it: re-authenticating would drop the attempt and the refusal would
        come from the wrong rule.
        """
        from odoo.addons.meli_oerp.models import meli_util

        other = self.env["res.users"].create({
            "name": "TD10-D other", "login": "td10_d_other",
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})

        session = {}
        with patch("odoo.http.request",
                   self._fake_request(self.admin.id, session)):
            nonce = meli_util.meli_oauth_attempt_issue(self.company_b)

        with patch("odoo.http.request",
                   self._fake_request(other.id, session)):
            ok, reason, company_id = meli_util.meli_oauth_attempt_consume(nonce)

        self.assertFalse(ok, "another user completed the attempt")
        self.assertEqual(reason, "the state belongs to another user",
                         "refused, but not for the reason under test")
        self.assertIsNone(company_id)

    def test_an_expired_attempt_is_refused(self):
        from odoo.addons.meli_oerp.models import meli_util

        session = {}
        with patch("odoo.http.request",
                   self._fake_request(self.admin.id, session)):
            nonce = meli_util.meli_oauth_attempt_issue(self.company_b)
            # Envejecer el intento sin esperar.
            with patch.object(meli_util, "_OAUTH_ATTEMPT_TTL_SECONDS", -1):
                ok, reason, _cid = meli_util.meli_oauth_attempt_consume(nonce)

        self.assertFalse(ok)
        self.assertEqual(reason, "the issued state expired")

    def test_a_fresh_attempt_inside_the_ttl_is_accepted(self):
        """Anti-vacuity for the expiry test."""
        from odoo.addons.meli_oerp.models import meli_util

        session = {}
        with patch("odoo.http.request",
                   self._fake_request(self.admin.id, session)):
            nonce = meli_util.meli_oauth_attempt_issue(self.company_b)
            ok, reason, company_id = meli_util.meli_oauth_attempt_consume(nonce)

        self.assertTrue(ok, "a fresh attempt was refused: %s" % reason)
        self.assertEqual(company_id, self.company_b.id)

    def test_without_an_http_session_the_attempt_fails_explicitly(self):
        """No degrading to an unbound state, and no inventing a session."""
        from odoo.addons.meli_oerp.models import meli_util

        with patch("odoo.http.request", None):
            with self.assertRaises(UserError):
                meli_util.meli_oauth_attempt_issue(self.company_b)

    # ==================================================================
    # authorisation still comes first
    # ==================================================================
    def test_an_unauthorised_caller_creates_no_attempt(self):
        """PR C refuses first, so nothing reaches the session."""
        user = self.env["res.users"].create({
            "name": "TD10-D plain", "login": "td10_d_plain",
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})

        session = {}
        counters = self._counters()
        fake = _FakeMeli(_SELLER_A)
        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            with patch("odoo.http.request",
                       self._fake_request(user.id, session)):
                with self.assertRaises(AccessError):
                    self.company_a.with_user(user).meli_login()
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(session, {},
                         "an unauthorised caller wrote an attempt")
        self.assertEqual(counters["client"], 0,
                         "an unauthorised caller built a client")

    # ==================================================================
    # nothing leaks
    # ==================================================================
    def test_the_success_page_carries_no_credential(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli(_SELLER_B)
        counters = self._counters()

        state = self._press_button(self.company_b, fake, counters)
        response = self._callback(state, fake, counters)

        for secret in (_NEW_ACCESS_B, _NEW_REFRESH_B, _FAKE_CODE,
                       "TD10_D_SECRET_CANARY", state):
            self.assertNotIn(secret, response.text,
                             "the response carries a secret or the state")

    # ==================================================================
    # the direct route is the other initiator, and it must gate the same way
    # ==================================================================
    def _open_direct(self, fake, counters):
        """GET /meli_login with no code: the direct start branch."""
        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            response = self.url_open("/meli_login", allow_redirects=False)
        finally:
            for p in patches:
                p.stop()
        return response

    def test_a_plain_user_cannot_start_a_flow_from_the_direct_route(self):
        """auth="user" is authentication, not authorisation.

        The start branch took the active company, reached _build_client -- a
        private capability that uses a narrow sudo() for the client secret and
        the auth row -- and handed back a usable OAuth URL. PR B's field groups
        arrive far too late: the capability boundary is already behind them.
        """
        self.authenticate("td10_d_internal", "td10_d_internal_pw")
        fake = _FakeMeli(_SELLER_A)
        counters = self._counters()

        response = self._open_direct(fake, counters)

        self.assertNotIn("auth.example", response.text,
                         "a plain user was handed an OAuth URL")
        self.assertNotIn("state=", response.text,
                         "a plain user was issued an attempt")
        self.assertEqual(counters["client"], 0,
                         "a plain user reached the capability boundary")
        self.assertEqual(counters["instance"], 0)
        self.assertEqual(counters["refresh"], 0)
        self.assertEqual(counters["probe"], 0)

    def test_a_system_user_can_still_start_from_the_direct_route(self):
        """Positive control: the gate must not close on the legitimate path."""
        self.authenticate("td10_d_system", "td10_d_system_pw")
        fake = _FakeMeli(_SELLER_A)
        counters = self._counters()

        response = self._open_direct(fake, counters)

        self.assertIn("state=", response.text,
                      "a System Administrator was refused the direct route")
        self.assertEqual(counters["client"], 1,
                         "the capability boundary was never reached, so the "
                         "zeros in the negative test prove nothing")

    def test_losing_the_admin_group_between_start_and_callback_fails_closed(self):
        """Authorisation is re-checked on the way back, not only on the way out.

        The callback only verified that the company belonged to the user, so
        someone who was an administrator when the flow started and is not one
        any more could still complete the exchange.
        """
        self.authenticate("td10_d_system", "td10_d_system_pw")
        fake = _FakeMeli(_SELLER_B)
        counters = self._counters()
        state = self._press_button(self.company_b, fake, counters)
        before = self._row(self.company_b)

        # Deja de ser administrador, con el intento ya emitido.
        self.system_user.write({
            "group_ids": [(3, self.env.ref("base.group_system").id)]})
        self.env.flush_all()
        self.assertFalse(self.system_user.has_group("base.group_system"),
                         "the group was not actually removed")

        counters["client"] = 0
        response = self._callback(state, fake, counters)

        self.assertNotIn("completed successfully", response.text)
        self.assertEqual(fake.authorize_calls, 0,
                         "the code was exchanged by a user who is no longer "
                         "an administrator")
        self.assertEqual(counters["client"], 0,
                         "a client was built before the refusal")
        self.assertEqual(self._row(self.company_b), before,
                         "credentials were written after losing the group")

        # El intento se consumio igual: no queda reutilizable.
        second = self._callback(state, fake, counters)
        self.assertNotIn("completed successfully", second.text,
                         "the consumed attempt survived the refusal")

    # ==================================================================
    # the attempt must carry an identity, not a None one
    # ==================================================================
    def test_an_attempt_without_a_resolvable_user_is_refused(self):
        """A session with no usable environment is not an identity.

        Storing "uid": None would let a later None == None comparison read an
        attempt that belongs to nobody as if it matched.
        """
        from odoo.addons.meli_oerp.models import meli_util

        # El env tiene que seguir siendo utilizable: UserError pasa su mensaje
        # por _(), que resuelve el idioma contra request.env. Lo que se simula
        # es un uid no resoluble, no un request roto.
        req = self._fake_request(self.admin.id)
        req.env = _NoUidEnv(req.env)
        with patch("odoo.http.request", req):
            with self.assertRaises(UserError):
                meli_util.meli_oauth_attempt_issue(self.company_b)

        self.assertEqual(dict(req.session), {},
                         "an attempt with no identity was written anyway")

    def test_a_consumer_without_a_resolvable_user_is_refused(self):
        from odoo.addons.meli_oerp.models import meli_util

        session = {}
        with patch("odoo.http.request",
                   self._fake_request(self.admin.id, session)):
            nonce = meli_util.meli_oauth_attempt_issue(self.company_b)

        req = self._fake_request(self.admin.id, session)
        req.env = _NoUidEnv(req.env)
        with patch("odoo.http.request", req):
            ok, reason, company_id = meli_util.meli_oauth_attempt_consume(nonce)

        self.assertFalse(ok, "an attempt was consumed with no current user")
        self.assertIsNone(company_id)
