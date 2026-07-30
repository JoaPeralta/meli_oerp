# -*- coding: utf-8 -*-
"""Only two surfaces may start a MercadoLibre authorization.

THE PATTERN
-----------
The OAuth attempt was secured at the two explicit initiators. But thirty-one
places in the connector did this:

    meli = self.env["meli.util"].get_new_instance(company)
    if meli.need_login():
        return meli.redirect_login()

``redirect_login()`` builds an authorization URL. So publishing a product,
pausing one, closing one, uploading an image, importing a category, printing a
shipment, fetching a question or a claim -- every one of them became an OAuth
initiator, reachable by whoever could run that commercial action, and none of
them went through the protected flow.

``product_meli_login`` was the clearest case: a public method on
``product.product``, reachable over RPC, with a button on the product's
MercadoLibre page carrying **no groups at all**.

WHAT IS AND IS NOT THE PROBLEM
------------------------------
Commercial actions using ``get_new_instance()`` is fine. When the connection
works they legitimately consume credentials.

The problem is what they did when it does **not** work: they minted an
authorization URL. Reconnecting is an administrative act, and it belongs to the
company configuration -- not to a product form.

THE CONTRACT
------------
    exactly two surfaces may build an authorization URL
    exactly two surfaces may issue an attempt

        res.company.meli_login
        /meli_login, the branch with no code

Everything else stops with one neutral message saying an administrator has to
reconnect the account from the company settings. The message names no URL, no
state, no seller, no company, no token, no secret and no code.

Connected behaviour is untouched.
"""

import ast
import glob
import json
import os
import tempfile
import textwrap
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

_SELLER_A = "2288636236"
_SELLER_B = "9999999999"
_A_ACCESS = "TD10_G_A_ACCESS_CANARY-%s" % _SELLER_A
_A_REFRESH = "TD10_G_A_REFRESH_CANARY"
_B_ACCESS = "TD10_G_B_ACCESS_CANARY-%s" % _SELLER_B
_B_REFRESH = "TD10_G_B_REFRESH_CANARY"
_SECRET_CANARY = "TD10_G_CLIENT_SECRET_CANARY"

# Las DOS unicas superficies productivas autorizadas a arrancar OAuth.
_CANONICAL = {
    ("models/company.py", "meli_login"),
    ("controllers/main.py", "index"),
}

# melisdk/ es una copia vendorizada cuyos imports estan todos comentados: no
# forma parte del codigo que corre. Se excluye a proposito, no por comodidad.
_SKIP_DIRS = ("tests", "melisdk", "migrations")


def _addon_root():
    from odoo.addons import meli_oerp

    return os.path.dirname(meli_oerp.__file__)


def _productive_files():
    root = _addon_root()
    found = []
    for path in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if rel.split("/")[0] in _SKIP_DIRS:
            continue
        found.append((rel, path))
    return sorted(found)


def _calls_of(attr_name):
    """Every productive call to `.<attr_name>(...)`, as (file, method, line)."""
    hits = []
    for rel, path in _productive_files():
        tree = ast.parse(open(path, encoding="utf-8").read())

        def visit(node, enclosing):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                elif isinstance(child, ast.ClassDef):
                    visit(child, enclosing)
                else:
                    if isinstance(child, ast.Call):
                        func = child.func
                        if (isinstance(func, ast.Attribute)
                                and func.attr == attr_name):
                            hits.append((rel, enclosing, child.lineno))
                        elif (isinstance(func, ast.Name)
                              and func.id == attr_name):
                            hits.append((rel, enclosing, child.lineno))
                    visit(child, enclosing)

        visit(tree, "<module>")
    return hits


class _CommercialMeli:
    """A client that says the connection needs authorization."""

    def __init__(self, counters, needs_login=True, seller=_SELLER_A):
        self._counters = counters
        self._needs_login = needs_login
        self.seller_id = seller
        self.access_token = _A_ACCESS
        self.refresh_token = _A_REFRESH
        self.client_id = "1111111111111111"
        self.client_secret = _SECRET_CANARY
        self.redirect_uri = "https://example.test/meli_login"
        self.AUTH_URL = "https://auth.example/authorization"
        self.needlogin_state = needs_login

    def need_login(self):
        return self._needs_login

    def auth_url(self, redirect_URI=None, state=None):
        self._counters["auth_url"] += 1
        return "https://auth.example/authorization?state=%s" % (state or "")

    def redirect_login(self):
        self._counters["redirect_login"] += 1
        return {"type": "ir.actions.act_url",
                "url": self.auth_url(), "target": "self"}

    def get(self, path, params=None, **kwargs):
        self._counters["business"] += 1
        return None

    def put_mini(self, path, body=None, params=None, **kwargs):
        self._counters["business"] += 1
        return None

    def post(self, *a, **kw):
        self._counters["business"] += 1
        return None

    def authorize(self, code, redirect_uri=None):
        self._counters["authorize"] += 1
        return {"access_token": "x", "refresh_token": "y",
                "user_id": int(self.seller_id), "expires_in": 21600}


@tagged("post_install", "-at_install")
class TestTd10NoImplicitOauthInitiators(HttpCase):

    def setUp(self):
        super().setUp()
        self.company_a = self.env.ref("base.main_company")
        self.company_b = self.env["res.company"].create(
            {"name": "TD10-G Company B"})
        for company, seller, access, refresh in (
            (self.company_a, _SELLER_A, _A_ACCESS, _A_REFRESH),
            (self.company_b, _SELLER_B, _B_ACCESS, _B_REFRESH),
        ):
            company.write({
                "mercadolibre_seller_id": seller,
                "mercadolibre_client_id": "1111111111111111",
                "mercadolibre_secret_key": _SECRET_CANARY,
                "mercadolibre_redirect_uri": "https://example.test/meli_login",
                "mercadolibre_access_token": access,
                "mercadolibre_refresh_token": refresh,
            })

        Groups = self.env["res.groups"]
        manager = self.env.ref("meli_oerp.group_mercadolibre_manager",
                               raise_if_not_found=False) or Groups
        self.ml_manager = self.env["res.users"].create({
            "name": "TD10-G ml manager", "login": "td10_g_manager",
            "password": "td10_g_manager_pw",
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id])],
            "group_ids": [(6, 0, ([self.env.ref("base.group_user").id]
                                  + (manager.ids if manager else [])))]})
        self.plain_user = self.env["res.users"].create({
            "name": "TD10-G internal", "login": "td10_g_internal",
            "password": "td10_g_internal_pw",
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})
        self.system_user = self.env["res.users"].create({
            "name": "TD10-G system", "login": "td10_g_system",
            "password": "td10_g_system_pw",
            "company_id": self.company_a.id,
            "company_ids": [(6, 0, [self.company_a.id, self.company_b.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id,
                                  self.env.ref("base.group_system").id])]})
        self.product = self.env["product.product"].create(
            {"name": "TD10-G product", "meli_id": "MLA-TD10-G"})
        self.env.flush_all()
        self.util = type(self.env["meli.util"])

    # ------------------------------------------------------------------
    def _counters(self):
        return {"auth_url": 0, "redirect_login": 0, "attempt": 0,
                "business": 0, "authorize": 0, "instance": 0, "client": 0}

    def _row(self, company):
        self.env.cr.execute(
            "SELECT access_token, refresh_token FROM mercadolibre_auth "
            "WHERE company_id = %s", (company.id,))
        return self.env.cr.fetchone()

    def _guarded(self, counters, fake):
        from odoo.addons.meli_oerp.controllers import main as controllers_main
        from odoo.addons.meli_oerp.models import company as company_module
        from odoo.addons.meli_oerp.models import meli_util

        original_issue = meli_util.meli_oauth_attempt_issue

        def get_new_instance(model, company=None, *a, **kw):
            counters["instance"] += 1
            return fake

        def build_client(model, company, *a, **kw):
            counters["client"] += 1
            return fake

        def attempt(company):
            counters["attempt"] += 1
            return original_issue(company)

        return (patch.object(self.util, "get_new_instance", get_new_instance),
                patch.object(self.util, "_build_client", build_client),
                patch.object(company_module, "meli_oauth_attempt_issue",
                             attempt),
                patch.object(controllers_main, "meli_oauth_attempt_issue",
                             attempt))

    def _run(self, callable_, counters, fake):
        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            try:
                return callable_(), None
            except Exception as exc:
                return None, exc
        finally:
            for p in patches:
                p.stop()

    def _rpc(self, model, method, ids, counters, fake):
        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            response = self.url_open(
                "/web/dataset/call_kw",
                data=json.dumps({
                    "jsonrpc": "2.0", "method": "call",
                    "params": {"model": model, "method": method,
                               "args": [ids], "kwargs": {}}}),
                headers={"Content-Type": "application/json"})
        finally:
            for p in patches:
                p.stop()
        return response.json()

    def _assert_started_no_oauth(self, counters, why):
        self.assertEqual(counters["auth_url"], 0,
                         "%s: built an authorization URL" % why)
        self.assertEqual(counters["redirect_login"], 0,
                         "%s: called redirect_login" % why)
        self.assertEqual(counters["attempt"], 0,
                         "%s: issued an OAuth attempt" % why)
        self.assertEqual(counters["authorize"], 0,
                         "%s: exchanged a code" % why)

    # ==================================================================
    # structural guards
    # ==================================================================
    def test_no_productive_code_calls_redirect_login(self):
        """The whole pattern, stated once.

        Any commercial path that answers `need_login()` with an authorization
        URL is an OAuth initiator, whether or not anyone meant it to be.
        """
        offenders = ["%s:%s():%d" % row for row in _calls_of("redirect_login")]

        self.assertEqual(
            offenders, [],
            "productive code still turns a commercial action into an OAuth "
            "initiator: %s" % offenders)

    def test_only_the_canonical_initiators_build_an_authorization_url(self):
        offenders = ["%s:%s():%d" % row for row in _calls_of("auth_url")
                     if (row[0], row[1]) not in _CANONICAL]

        self.assertEqual(
            offenders, [],
            "an authorization URL is built outside the two canonical "
            "initiators: %s" % offenders)

    def test_only_the_canonical_initiators_issue_an_attempt(self):
        offenders = [
            "%s:%s():%d" % row
            for row in _calls_of("meli_oauth_attempt_issue")
            if (row[0], row[1]) not in _CANONICAL
        ]

        self.assertEqual(
            offenders, [],
            "an OAuth attempt is issued outside the two canonical "
            "initiators: %s" % offenders)

    def test_the_guard_can_actually_find_calls(self):
        """Anti-vacuity: an empty scanner would pass every guard above."""
        self.assertTrue(_productive_files(),
                        "the scanner found no productive files at all")
        self.assertTrue(_calls_of("get_new_instance"),
                        "the scanner cannot find calls, so the guards above "
                        "prove nothing")

    # ==================================================================
    # product_meli_login is not an initiator any more
    # ==================================================================
    def test_an_ml_manager_cannot_start_oauth_from_a_product(self):
        self.authenticate("td10_g_manager", "td10_g_manager_pw")
        counters = self._counters()
        fake = _CommercialMeli(counters)

        payload = self._rpc("product.product", "product_meli_login",
                            [self.product.id], counters, fake)

        result = json.dumps(payload)
        self.assertNotIn("auth.example", result,
                         "an ML Manager was handed an OAuth URL")
        self._assert_started_no_oauth(counters, "an ML Manager on a product")
        self.assertEqual(counters["instance"], 0,
                         "the authenticated boundary was crossed")
        self.assertEqual(counters["client"], 0, "a client was built")

    def test_a_plain_user_cannot_start_oauth_from_a_product(self):
        self.authenticate("td10_g_internal", "td10_g_internal_pw")
        counters = self._counters()
        fake = _CommercialMeli(counters)

        payload = self._rpc("product.product", "product_meli_login",
                            [self.product.id], counters, fake)

        self.assertNotIn("auth.example", json.dumps(payload))
        self._assert_started_no_oauth(counters, "a plain user on a product")

    def test_the_product_button_is_gone_from_the_form(self):
        import xml.etree.ElementTree as ET

        path = os.path.join(_addon_root(), "views", "product_view.xml")
        names = [b.get("name") for b in ET.parse(path).getroot().iter("button")]

        self.assertNotIn(
            "product_meli_login", names,
            "the product form still offers an OAuth entry point")

    # ==================================================================
    # commercial actions stop instead of starting OAuth
    # ==================================================================
    def test_a_commercial_product_action_stops_without_starting_oauth(self):
        counters = self._counters()
        fake = _CommercialMeli(counters, needs_login=True)

        _result, raised = self._run(
            lambda: self.product.product_meli_status_pause(), counters, fake)

        self.assertIsInstance(
            raised, UserError,
            "a commercial action did not stop explicitly (got %r)" % raised)
        self._assert_started_no_oauth(counters, "pausing a product")
        self.assertEqual(counters["business"], 0,
                         "the remote business operation ran anyway")

    def test_a_company_commercial_action_stops_without_starting_oauth(self):
        counters = self._counters()
        fake = _CommercialMeli(counters, needs_login=True)

        _result, raised = self._run(
            lambda: self.company_a.product_meli_get_products(), counters, fake)

        self.assertIsInstance(raised, UserError)
        self._assert_started_no_oauth(counters, "importing products")

    def test_the_stop_message_names_nothing_sensitive(self):
        counters = self._counters()
        fake = _CommercialMeli(counters, needs_login=True)

        _result, raised = self._run(
            lambda: self.product.product_meli_status_pause(), counters, fake)

        message = str(raised)
        for secret, label in ((_A_ACCESS, "the access token"),
                              (_A_REFRESH, "the refresh token"),
                              (_SECRET_CANARY, "the client secret"),
                              (_SELLER_A, "the seller id"),
                              ("auth.example", "an authorization URL"),
                              ("state=", "a state")):
            self.assertNotIn(secret, message,
                             "the stop message names %s" % label)

    def test_a_connected_commercial_action_still_works(self):
        """Positive control: the actions were stopped, not switched off."""
        counters = self._counters()
        fake = _CommercialMeli(counters, needs_login=False)

        _result, raised = self._run(
            lambda: self.product.product_meli_status_pause(), counters, fake)

        self.assertIsNone(raised,
                          "a connected commercial action broke: %r" % raised)
        self.assertGreaterEqual(
            counters["business"], 1,
            "the commercial backend was never reached, so the stop tests "
            "above prove nothing")

    def test_a_commercial_action_on_another_company_starts_nothing(self):
        """Company A is active; the action is about B, which needs login."""
        counters = self._counters()
        fake = _CommercialMeli(counters, needs_login=True, seller=_SELLER_B)
        before_a, before_b = self._row(self.company_a), self._row(self.company_b)

        _result, raised = self._run(
            lambda: self.company_b.with_user(
                self.system_user).product_meli_get_products(),
            counters, fake)

        self.assertIsInstance(raised, UserError)
        self._assert_started_no_oauth(counters, "a commercial action about B")
        self.assertEqual(self._row(self.company_a), before_a,
                         "company A's credentials changed")
        self.assertEqual(self._row(self.company_b), before_b,
                         "company B's credentials changed")

    # ==================================================================
    # the canonical initiators still work
    # ==================================================================
    def test_the_company_button_still_issues_exactly_one_attempt(self):
        self.authenticate("td10_g_system", "td10_g_system_pw")
        counters = self._counters()
        fake = _CommercialMeli(counters, needs_login=True)

        payload = self._rpc("res.company", "meli_login",
                            [self.company_a.id], counters, fake)

        self.assertNotIn("error", payload,
                         "the canonical initiator broke: %s"
                         % str(payload.get("error"))[:200])
        self.assertEqual(counters["attempt"], 1,
                         "expected exactly one attempt")
        self.assertEqual(counters["auth_url"], 1,
                         "expected exactly one authorization URL")

    def test_the_direct_entry_point_still_issues_exactly_one_attempt(self):
        self.authenticate("td10_g_system", "td10_g_system_pw")
        counters = self._counters()
        fake = _CommercialMeli(counters, needs_login=True)
        patches = self._guarded(counters, fake)
        try:
            for p in patches:
                p.start()
            response = self.url_open("/meli_login", allow_redirects=False)
        finally:
            for p in patches:
                p.stop()

        self.assertIn("state=", response.text)
        self.assertEqual(counters["attempt"], 1)
        self.assertEqual(counters["auth_url"], 1)

    # ==================================================================
    # the scanner has to say WHICH index
    # ==================================================================
    def test_the_scanner_distinguishes_methods_with_the_same_name(self):
        """Four classes in controllers/main.py define a method called `index`.

        Allowing them by bare function name allows all four, and only
        MercadoLibreLogin.index is canonical. The scanner has to qualify the
        name with its class or the allowlist means nothing.
        """
        source = textwrap.dedent("""
            class Public:
                def index(self):
                    client.auth_url()

            class Login:
                def index(self):
                    client.auth_url()
        """)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "synthetic.py")
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write(source)

            with patch("odoo.addons.meli_oerp.tests."
                       "test_td10_no_implicit_oauth_initiators."
                       "_productive_files",
                       return_value=[("synthetic.py", path)]):
                found = _calls_of("auth_url")

        names = sorted(name for _f, name, _l in found)
        self.assertEqual(
            names, ["Login.index", "Public.index"],
            "the scanner cannot tell two methods called `index` apart, so the "
            "allowlist cannot single out the canonical one: %s" % names)

    def test_every_index_in_the_controller_is_named_apart(self):
        """The real file, not a synthetic one."""
        root = _addon_root()
        tree = ast.parse(open(os.path.join(root, "controllers", "main.py"),
                              encoding="utf-8").read())
        indexes = sorted(
            "%s.index" % cls.name
            for cls in tree.body if isinstance(cls, ast.ClassDef)
            for fn in cls.body
            if isinstance(fn, ast.FunctionDef) and fn.name == "index")

        self.assertEqual(
            indexes,
            ["MercadoLibre.index", "MercadoLibreAuthorize.index",
             "MercadoLibreLogin.index", "MercadoLibreLogout.index"],
            "the controller's index methods are not what the allowlist "
            "assumes: %s" % indexes)
        canonical = {name for _f, name in _CANONICAL}
        self.assertIn("MercadoLibreLogin.index", canonical)
        for other in ("MercadoLibre.index", "MercadoLibreAuthorize.index",
                      "MercadoLibreLogout.index"):
            self.assertNotIn(
                other, canonical,
                "%s is allowed to start an authorization, and it must not be"
                % other)

    # ==================================================================
    # nothing inherited a decorator from a removed method
    # ==================================================================
    def test_convert_to_datetime_kept_its_own_decorators(self):
        """Deleting the method above it must not hand over its decorator.

        get_url_meli_login carried @api.model. Removing the method without its
        decorator line would leave that decorator applied to whatever came
        next, silently changing an unrelated API.
        """
        root = _addon_root()
        tree = ast.parse(open(os.path.join(root, "models", "meli_util.py"),
                              encoding="utf-8").read())
        found = [fn for fn in ast.walk(tree)
                 if isinstance(fn, ast.FunctionDef)
                 and fn.name == "convert_to_datetime"]

        self.assertTrue(found, "convert_to_datetime disappeared")
        for fn in found:
            names = [d.attr if isinstance(d, ast.Attribute)
                     else getattr(d, "id", str(d))
                     for d in fn.decorator_list]
            self.assertEqual(
                names, [],
                "convert_to_datetime inherited a decorator from a removed "
                "method: %s" % names)
