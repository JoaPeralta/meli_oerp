# -*- coding: utf-8 -*-
"""The OAuth callback must prove the code it receives answers a request we made.

THE DEFECT
----------
``auth_url()`` emitted a ``state`` parameter and the callback never read it:

    random_id = str(now)
    params = {..., 'state': random_id}

Two problems in one line. The value was ``str(datetime.now())`` -- guessable to
the second, not random -- and nothing on the way back checked it. So the
callback would exchange **any** authorization code delivered to it, from any
origin, as long as an Odoo user was logged in. That is the shape of a CSRF: an
attacker who can make the victim's browser hit ``/meli_login?code=...`` gets
their own MercadoLibre account bound to the victim's company.

THE CONTRACT
------------
The state is issued by the controller, not by the client object, and is:

    random        secrets.token_urlsafe, not a timestamp
    session-bound stored in this user's Odoo session, so another session's
                  state cannot be replayed here
    expirable     an issued state stops being acceptable after a while
    single-use    consumed on first validation, so a captured redirect cannot
                  be replayed

It is validated and consumed **before** the code is exchanged. A missing,
unknown, expired or already-used state means no exchange happens at all --
``authorize`` is never called, so no credential is ever requested, let alone
stored.
"""

import re
import time
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

_SELLER = "2288636236"
_FAKE_CODE = "TG-FAKE-AUTH-CODE-0002"
_FAKE_ACCESS = "APPUSR-FAKE-ACCESS-0002-%s" % _SELLER
_FAKE_REFRESH = "TG-FAKE-REFRESH-0002"
_STORED_ACCESS = "PREEXISTING-ACCESS-0000"
_STORED_REFRESH = "PREEXISTING-REFRESH-0000"

_SESSION_KEY = "meli_oauth_state"


class _FakeMeli:
    def __init__(self):
        self.access_token = _FAKE_ACCESS
        self.refresh_token = _FAKE_REFRESH
        self.authorize_calls = 0
        self.last_state = None

    def authorize(self, code, redirect_uri=None):
        self.authorize_calls += 1
        return {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH,
                "token_type": "Bearer", "expires_in": 21600,
                "user_id": int(_SELLER)}

    def auth_url(self, redirect_URI=None, state=None):
        self.last_state = state
        return "https://auth.example/authorization?state=%s" % (state or "")

    def get(self, path, params=None, **kwargs):
        return None


@tagged("post_install", "-at_install")
class TestOAuthStateProtection(HttpCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.company.write({
            "mercadolibre_redirect_uri": "https://example.test/meli_login",
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": "client-secret-value-4444444444",
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _STORED_ACCESS,
            "mercadolibre_refresh_token": _STORED_REFRESH,
        })
        self.env.flush_all()

    # ------------------------------------------------------------------
    def _stored(self):
        self.env.cr.execute(
            "SELECT access_token, refresh_token FROM mercadolibre_auth "
            "WHERE company_id = %s", (self.company.id,))
        row = self.env.cr.fetchone()
        return row if row else (None, None)

    def _issue_state(self, fake):
        """Hit the entry point that renders a login link, which issues a state.

        The value is read back out of the rendered link rather than off the
        fake, so the test asserts what a browser would actually receive.
        """
        with patch.object(type(self.env["meli.util"]), "_build_client",
                          return_value=fake):
            response = self.url_open("/meli_login", allow_redirects=False)
        found = re.search(r"state=([A-Za-z0-9_\-]+)", response.text)
        return found.group(1) if found else None

    def _callback(self, fake, state=None, code=_FAKE_CODE):
        url = "/meli_login?code=%s" % code
        if state is not None:
            url += "&state=%s" % state
        with patch.object(type(self.env["meli.util"]), "_build_client",
                          return_value=fake):
            response = self.url_open(url, allow_redirects=False)
        self.env.invalidate_all()
        return response

    def _assert_no_exchange(self, fake, why):
        self.assertEqual(
            fake.authorize_calls, 0,
            "%s: the authorization code was exchanged anyway" % why)
        access, refresh = self._stored()
        self.assertEqual(access, _STORED_ACCESS,
                         "%s: credentials were overwritten" % why)
        self.assertEqual(refresh, _STORED_REFRESH, "%s: credentials were "
                         "overwritten" % why)

    # ------------------------------------------------------------------
    # the state itself
    # ------------------------------------------------------------------
    def test_the_issued_state_is_random_not_a_timestamp(self):
        """`str(datetime.now())` is guessable to the second."""
        self.authenticate("admin", "admin")
        first = self._issue_state(_FakeMeli())
        second = self._issue_state(_FakeMeli())

        self.assertTrue(first, "no state was issued")
        self.assertNotEqual(first, second,
                            "two authorization URLs carried the same state")
        self.assertNotIn(":", first,
                         "the state still looks like a timestamp")
        self.assertGreaterEqual(len(first), 20,
                                "the state is too short to be unguessable")

    # ------------------------------------------------------------------
    # rejections, all before the exchange
    # ------------------------------------------------------------------
    def test_a_missing_state_is_rejected_before_the_exchange(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli()
        self._issue_state(fake)

        self._callback(fake, state=None)

        self._assert_no_exchange(fake, "a callback with no state")

    def test_an_unknown_state_is_rejected_before_the_exchange(self):
        """The CSRF case: a code delivered by someone else."""
        self.authenticate("admin", "admin")
        fake = _FakeMeli()
        self._issue_state(fake)

        self._callback(fake, state="attacker-supplied-value")

        self._assert_no_exchange(fake, "a callback with a foreign state")

    def test_a_callback_without_any_issued_state_is_rejected(self):
        """Nothing was ever issued in this session, so nothing can match."""
        self.authenticate("admin", "admin")
        fake = _FakeMeli()

        self._callback(fake, state="anything-at-all")

        self._assert_no_exchange(fake, "a session that issued no state")

    def test_a_state_cannot_be_used_twice(self):
        """A captured redirect must not be replayable."""
        self.authenticate("admin", "admin")
        fake = _FakeMeli()
        state = self._issue_state(fake)

        first = self._callback(fake, state=state)
        self.assertEqual(fake.authorize_calls, 1,
                         "the first, legitimate exchange did not happen")

        second_fake = _FakeMeli()
        self._callback(second_fake, state=state)

        self.assertEqual(
            second_fake.authorize_calls, 0,
            "the same state was accepted a second time; a captured redirect "
            "would be replayable")

    def test_an_expired_state_is_rejected(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli()
        state = self._issue_state(fake)

        # Age the issued state past its window without waiting for it.
        from odoo.addons.meli_oerp.controllers import main as controllers_main
        with patch.object(controllers_main, "_OAUTH_STATE_TTL_SECONDS", -1):
            self._callback(fake, state=state)

        self._assert_no_exchange(fake, "a state past its expiry")

    # ------------------------------------------------------------------
    # the legitimate flow still works
    # ------------------------------------------------------------------
    def test_a_matching_state_lets_the_exchange_through(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli()
        state = self._issue_state(fake)

        self._callback(fake, state=state)

        self.assertEqual(fake.authorize_calls, 1)
        access, refresh = self._stored()
        self.assertEqual(access, _FAKE_ACCESS)
        self.assertEqual(refresh, _FAKE_REFRESH)

    def test_the_state_is_not_echoed_into_the_response(self):
        self.authenticate("admin", "admin")
        fake = _FakeMeli()
        state = self._issue_state(fake)

        response = self._callback(fake, state=state)

        self.assertNotIn(_FAKE_ACCESS, response.text)
        self.assertNotIn(_FAKE_REFRESH, response.text)
        self.assertNotIn(_FAKE_CODE, response.text)
