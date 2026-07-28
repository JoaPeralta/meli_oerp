# -*- coding: utf-8 -*-
"""Disconnecting MercadoLibre is destructive, so it must say so first.

THE DECISION
------------
``meli_logout`` stays destructive. Keeping the tokens would mean the account is
still genuinely connected, and an action called "disconnect" that leaves a live
session behind is a worse lie than the one being fixed here.

What is wrong is the framing. The button read:

    string="Cerrar sesión"   title="Cerrar sesión con ML"

"Cerrar sesión" is what you click when you are done for the day and expect to
log back in. This is not that. MercadoLibre's refresh tokens are single-use, so
the moment the row is blanked the stored credential is gone for good and only a
fresh OAuth authorisation brings the connector back. One click, no warning, and
the way back is a manual round trip.

THE CONTRACT
------------
    named           "Desconectar MercadoLibre de Odoo", not "Cerrar sesión"
    confirmed       a real Odoo confirmation, not help text
    scoped          clears access_token, refresh_token and code -- nothing else
    local           never calls MercadoLibre, never revokes remotely
    atomic          either the three go, or none does
    quiet           no refresh, no POST to /oauth/token

Everything that is not a credential survives: seller_id, client_id, secret,
redirect_uri, cron configuration, commercial settings, postings, orders, stock
and price.

WHOSE CREDENTIALS
-----------------
The method called ``ensure_one()`` and then acted on
``self.env.user.company_id`` -- the user's *active* company, not the record the
button was pressed on. Those differ as soon as there is more than one company,
so the confirmation could describe one account and disconnect another. A
confirmation dialog that lies about its target is not a hardening, so the method
now acts on the record it was invoked on.
"""

import hashlib
import os
import xml.etree.ElementTree as ET
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_SELLER = "2288636236"
_ACCESS = "FAKE-ACCESS-000000000000-%s" % _SELLER
_REFRESH = "FAKE-REFRESH-111111111111"
_CODE = "FAKE-CODE-222222222222"
_CLIENT_ID = "1234567890123456"
_SECRET = "client-secret-value-4444444444"
_REDIRECT = "https://example.test/meli_login"

_EXPECTED_LABEL = "Desconectar MercadoLibre de Odoo"


def _fp(value):
    """Fingerprint, never the value."""
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _button(name):
    """The rendered button as the form actually declares it."""
    from odoo.addons import meli_oerp

    path = os.path.join(os.path.dirname(meli_oerp.__file__),
                        "views", "company_view.xml")
    root = ET.parse(path).getroot()
    for element in root.iter("button"):
        if element.get("name") == name:
            return element
    return None


@tagged("post_install", "-at_install")
class TestExplicitMeliDisconnect(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _ACCESS,
            "mercadolibre_refresh_token": _REFRESH,
            "mercadolibre_code": _CODE,
            "mercadolibre_client_id": _CLIENT_ID,
            "mercadolibre_secret_key": _SECRET,
            "mercadolibre_redirect_uri": _REDIRECT,
        })
        self.env.flush_all()

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

    def _disconnect(self, record=None):
        """Run the action, counting anything that would reach MercadoLibre."""
        util = type(self.env["meli.util"])
        counters = {"refresh": 0, "client": 0}
        original_refresh = util._meli_refresh_credentials

        def counting_refresh(*args, **kwargs):
            counters["refresh"] += 1
            return original_refresh(*args, **kwargs)

        def counting_client(*args, **kwargs):
            counters["client"] += 1
            raise AssertionError(
                "a local disconnect must not build an API client")

        with patch.object(util, "_meli_refresh_credentials", counting_refresh), \
                patch.object(util, "get_new_instance", counting_client):
            result = (record or self.company).meli_logout()
        self.env.flush_all()
        self.env.invalidate_all()
        return result, counters

    # ------------------------------------------------------------------
    # not invoked: nothing happens
    # ------------------------------------------------------------------
    def test_untouched_credentials_survive_ordinary_writes(self):
        """Nothing clears the credentials on its own."""
        before = self._auth_row()

        self.company.write({"mercadolibre_official_store_id": 7})
        self.env.flush_all()
        self.env.invalidate_all()

        self.assertEqual(self._auth_row(), before,
                         "an unrelated write cleared the credentials")

    # ------------------------------------------------------------------
    # invoked explicitly: exactly the three fields go
    # ------------------------------------------------------------------
    def test_an_explicit_disconnect_clears_the_three_credentials(self):
        self._disconnect()

        after = self._auth_row()
        self.assertIsNotNone(after, "the auth row itself was deleted")
        self.assertIsNone(after["access"], "the access token survived")
        self.assertIsNone(after["refresh"], "the refresh token survived")
        self.assertIsNone(after["code"], "the authorization code survived")

    def test_everything_that_is_not_a_credential_survives(self):
        self._disconnect()

        self.assertEqual(self.company.mercadolibre_seller_id, _SELLER,
                         "the seller id was cleared")
        self.assertEqual(self.company.mercadolibre_client_id, _CLIENT_ID,
                         "the client id was cleared")
        self.assertEqual(self.company.mercadolibre_secret_key, _SECRET,
                         "the client secret was cleared")
        self.assertEqual(self.company.mercadolibre_redirect_uri, _REDIRECT,
                         "the redirect uri was cleared")

    def test_the_disconnect_is_local_and_never_calls_mercadolibre(self):
        """No remote revocation. Documented as a local disconnect."""
        _result, counters = self._disconnect()

        self.assertEqual(counters["client"], 0,
                         "the disconnect built an API client")
        self.assertEqual(counters["refresh"], 0,
                         "the disconnect renewed the credentials")

    def test_it_disconnects_the_company_it_was_invoked_on(self):
        """The confirmation must not describe one account and clear another."""
        other = self.env["res.company"].create({
            "name": "Segunda empresa (test)",
            "mercadolibre_seller_id": "9999999999",
            "mercadolibre_access_token": "OTHER-ACCESS-999",
            "mercadolibre_refresh_token": "OTHER-REFRESH-999",
        })
        self.env.flush_all()
        before = self._auth_row()

        self._disconnect(record=other)

        self.assertEqual(
            self._auth_row(), before,
            "disconnecting one company cleared another company's credentials")

    # ------------------------------------------------------------------
    # atomicity
    # ------------------------------------------------------------------
    def test_a_failure_midway_leaves_no_half_disconnected_state(self):
        """Either the three go, or none does."""
        original = type(self.company)._meli_write_auth_field
        seen = {"n": 0}

        def exploding(recordset, auth_field, facade_field, empty=False):
            seen["n"] += 1
            if seen["n"] == 2:
                raise ValueError("persistence blew up midway")
            return original(recordset, auth_field, facade_field, empty=empty)

        before = self._auth_row()
        with patch.object(type(self.company), "_meli_write_auth_field",
                          exploding):
            with self.assertRaises(ValueError):
                with self.env.cr.savepoint():
                    self.company.meli_logout()
        self.env.invalidate_all()

        self.assertGreaterEqual(
            seen["n"], 2,
            "the failure never happened, so this test proves nothing")
        self.assertEqual(
            self._auth_row(), before,
            "a failed disconnect left a half-cleared credential set behind")

    # ------------------------------------------------------------------
    # the visible button
    # ------------------------------------------------------------------
    def test_the_button_says_what_it_does(self):
        button = _button("meli_logout")

        self.assertIsNotNone(button, "the disconnect button is gone from the form")
        self.assertEqual(
            button.get("string"), _EXPECTED_LABEL,
            "the button still reads like an innocuous log out")

    def test_the_button_asks_for_confirmation_before_running(self):
        """A real Odoo confirmation, not help text nobody reads."""
        button = _button("meli_logout")
        confirm = (button is not None and button.get("confirm")) or ""

        self.assertTrue(
            confirm.strip(),
            "the button destroys the stored credentials with no confirmation")
        for expected in ("credenciales", "OAuth"):
            self.assertIn(
                expected, confirm,
                "the confirmation does not say that reconnecting needs a new "
                "OAuth authorisation: %r" % confirm)
