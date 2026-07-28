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

import re
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase


_SELLER = "2288636236"
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
        # A successful code->token exchange. user_id is what identifies the
        # account, and the callback refuses to store a response without it.
        return {"access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "token_type": "Bearer", "expires_in": 21600,
                "user_id": int(_SELLER)}

    def auth_url(self, redirect_URI=None, state=None):
        # El state va en la URL porque es de ahi que el test lo lee de vuelta,
        # igual que haria un navegador.
        return "https://auth.example/authorization?state=%s" % (state or "")

    def get(self, path, params=None, **kwargs):
        # The callback resolves AUTH_URL, which walks get_ML_AUTH_URL ->
        # _get_ML_sites -> GET /sites. Returning nothing makes that fall back to
        # the currency lookup, and keeps this test off the network.
        return None


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
            # The callback now verifies which account answered before storing
            # anything, so this fixture has to name the seller it expects.
            "mercadolibre_seller_id": _SELLER,
        })

    def test_callback_hides_secrets_and_stores_tokens(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli()

        with patch.object(
            type(self.env["meli.util"]), "_build_client", return_value=fake
        ):
            # The callback now requires a state issued by this session.
            issued = self.url_open("/meli_login", allow_redirects=False)
            self.assertEqual(
                issued.status_code, 200,
                "the login entry point failed (%s), so no state was issued"
                % issued.status_code)
            found = re.search(r"state=([A-Za-z0-9_\-]+)", issued.text)
            state = found.group(1) if found else ""
            response = self.url_open(
                "/meli_login?code=%s&state=%s" % (_FAKE_CODE, state),
                allow_redirects=False
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
