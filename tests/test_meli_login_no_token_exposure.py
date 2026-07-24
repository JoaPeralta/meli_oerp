# -*- coding: utf-8 -*-
"""Regression test: the /meli_login OAuth callback must not expose secrets.

After a successful OAuth exchange, the callback used to render the authorization
code, the access token and the refresh token straight into the HTML response.
This test drives the callback with a fake MercadoLibre client (no network, no
real credentials) and asserts that:

* the response indicates success (HTTP 200 + a neutral confirmation message);
* the response body contains NONE of: authorization code, access token, refresh
  token, client secret;
* the tokens are nevertheless stored server-side on res.company.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase


_FAKE_CODE = "FAKEAUTHCODE0001"
_FAKE_ACCESS = "APPUSR-FAKE-ACCESS-0001"
_FAKE_REFRESH = "TG-FAKE-REFRESH-0001"
_FAKE_SECRET = "FAKE-CLIENT-SECRET-0001"


class _FakeMeli:
    """Stand-in for the MercadoLibre client returned by get_new_instance."""

    def __init__(self):
        self.access_token = _FAKE_ACCESS
        self.refresh_token = _FAKE_REFRESH

    def authorize(self, code, redirect_uri=None):
        # Simulate a successful code->token exchange (tokens already set).
        return {"access_token": self.access_token, "refresh_token": self.refresh_token}

    def auth_url(self, redirect_URI=None):
        return "https://auth.example/authorization"


@tagged("post_install", "-at_install")
class TestMeliLoginNoTokenExposure(HttpCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.company.write({
            "mercadolibre_redirect_uri": "https://example.test/meli_login",
            "mercadolibre_secret_key": _FAKE_SECRET,
            "mercadolibre_access_token": "",
            "mercadolibre_refresh_token": "",
        })

    def test_callback_hides_secrets_and_stores_tokens(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli()

        with patch.object(
            type(self.env["meli.util"]), "get_new_instance", return_value=fake
        ):
            response = self.url_open(
                "/meli_login?code=%s" % _FAKE_CODE, allow_redirects=False
            )

        self.assertEqual(response.status_code, 200)
        body = response.text

        # Success is communicated without leaking any secret.
        self.assertIn("completed successfully", body)
        self.assertNotIn(_FAKE_ACCESS, body, "access token leaked in OAuth callback response")
        self.assertNotIn(_FAKE_REFRESH, body, "refresh token leaked in OAuth callback response")
        self.assertNotIn(_FAKE_CODE, body, "authorization code leaked in OAuth callback response")
        self.assertNotIn(_FAKE_SECRET, body, "client secret leaked in OAuth callback response")

        # Tokens must still be persisted server-side on the company.
        self.env.invalidate_all()
        self.assertEqual(self.company.mercadolibre_access_token, _FAKE_ACCESS)
        self.assertEqual(self.company.mercadolibre_refresh_token, _FAKE_REFRESH)
