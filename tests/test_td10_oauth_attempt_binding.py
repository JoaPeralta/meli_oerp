# -*- coding: utf-8 -*-
"""Evidence probe for PR D: where does the OAuth attempt actually begin?

PR D has to bind the attempt to a uid and a company. That is only possible at
the point where the attempt is created. This file establishes, behaviourally,
which entry points create one today.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

_SELLER = "2288636236"


class _FakeMeli:
    def __init__(self):
        self.access_token = "TD10_D_ACCESS_CANARY"
        self.refresh_token = "TD10_D_REFRESH_CANARY"
        self.seller_id = _SELLER
        self.AUTH_URL = "https://auth.example/authorization"
        self.client_id = "1111111111111111"
        self.redirect_uri = "https://example.test/meli_login"
        self.authorize_calls = 0

    def need_login(self):
        return True

    def auth_url(self, redirect_URI=None, state=None):
        return "https://auth.example/authorization?state=%s" % (state or "")

    def redirect_login(self):
        return {"type": "ir.actions.act_url", "url": str(self.auth_url()),
                "target": "self"}

    def authorize(self, code, redirect_uri=None):
        self.authorize_calls += 1
        return {"access_token": "x", "refresh_token": "y",
                "user_id": int(_SELLER), "expires_in": 21600}

    def get(self, path, params=None, **kwargs):
        return None


@tagged("post_install", "-at_install")
class TestTd10OauthAttemptBinding(HttpCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_client_id": "1111111111111111",
            "mercadolibre_secret_key": "TD10_D_SECRET_CANARY",
            "mercadolibre_redirect_uri": "https://example.test/meli_login",
        })
        self.env.flush_all()

    def test_the_button_creates_an_attempt_the_callback_can_match(self):
        """meli_login is the entry point PR C secured, so it is where an
        attempt should be created. If it creates none, PR D has nothing to
        bind a uid and a company to."""
        self.authenticate("admin", "admin")
        fake = _FakeMeli()

        util = type(self.env["meli.util"])
        with patch.object(util, "get_new_instance", return_value=fake):
            action = self.env["res.company"].browse(
                self.company.id).meli_login()

        url = action.get("url", "")
        self.assertIn("state=", url, "the login action carries no state at all")
        state = url.split("state=", 1)[1].split("&")[0]

        # Lo decisivo: ese state tiene que poder cerrar el circuito.
        with patch.object(util, "_build_client", return_value=fake):
            response = self.url_open(
                "/meli_login?code=TD10-D-FAKE-CODE&state=%s" % state,
                allow_redirects=False)

        self.assertIn(
            "completed successfully", response.text,
            "the callback refused the state that the login button itself "
            "produced, so the button flow cannot complete OAuth at all")
