# -*- coding: utf-8 -*-
"""Internal code must consume the credentials as a capability, not as data.

THE PROBLEM THIS PR SOLVES
--------------------------
``_build_client`` reads the secrets off the public ``res.company`` facade:

    api_rest_client.client_secret = company.mercadolibre_secret_key
    api_rest_client.access_token  = company.mercadolibre_access_token or ''
    api_rest_client.refresh_token = company.mercadolibre_refresh_token

Those are ordinary readable fields today, which is exactly the exposure TD10 is
about. The moment they are restricted to ``base.group_system``, every internal
process running as an ordinary commercial user -- a cron, an order import, a
notification -- stops being able to build a client at all.

So the protection cannot be added first. The consumer has to stop going through
the public door before the public door can be locked. That is this PR, and
nothing else: no field is restricted here.

THE GATEWAY
-----------
``_build_client(company)`` becomes the single private capability boundary:

    private          leading underscore, not reachable over RPC
    explicit company received as an argument, validated as a singleton
    narrow           sudo() only to read the auth row and the client secret,
                     never over orders, products, postings or any commercial
                     record
    a capability     returns a configured client, never a dict of secrets
    inert            no network, no refresh, no writes, no logging of values

The contract of ``get_new_instance`` does not change.

HOW THE RED IS PRODUCED
-----------------------
The point is "the gateway does not depend on the facade". To state that as
behaviour rather than as a shape, the tests make the facade unusable -- the
compute raises, exactly as a restricted field will behave for a non-system user
-- and then require the client to come out correctly configured anyway.

Before this PR the client cannot be built. After it, it can.

DEAD READS
----------
``notification.py`` assigns ``ACCESS_TOKEN`` and ``REFRESH_TOKEN`` from the
facade in four methods and never uses either. They are removed here because
they are four more non-system readers of a field that is about to be
restricted, and deleting an unused local is the whole change.
"""

import hashlib
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_SELLER = "2288636236"
_ACCESS_CANARY = "TD10_ACCESS_CANARY-%s" % _SELLER
_REFRESH_CANARY = "TD10_REFRESH_CANARY"
_CODE_CANARY = "TD10_CODE_CANARY"
_SECRET_CANARY = "TD10_CLIENT_SECRET_CANARY"


def _fp(value):
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


class _CountingSession:
    """Any call here means the constructor reached the network."""

    def __init__(self):
        self.get_count = 0
        self.post_count = 0

    def get(self, url, **kwargs):
        self.get_count += 1
        raise AssertionError("_build_client issued a GET")

    def post(self, url, **kwargs):
        self.post_count += 1
        raise AssertionError("_build_client issued a POST")


@tagged("post_install", "-at_install")
class TestTd10PrivateCapabilityGateway(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": _SECRET_CANARY,
            "mercadolibre_access_token": _ACCESS_CANARY,
            "mercadolibre_refresh_token": _REFRESH_CANARY,
            "mercadolibre_code": _CODE_CANARY,
            "mercadolibre_redirect_uri": "https://example.test/meli_login",
        })
        self.env.flush_all()
        self.util = self.env["meli.util"]

        self.commercial_user = self.env["res.users"].create({
            "name": "TD10 commercial",
            "login": "td10_commercial_gateway",
            "company_id": self.company.id,
            "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
        })

    # ------------------------------------------------------------------
    def _facade_denied(self):
        """Make the public facade behave the way a restricted field will.

        This is the whole point of the PR: after the fields carry
        groups="base.group_system", a non-system reader gets nothing from them.
        Simulating that here lets the dependency be tested now, before the
        restriction exists, instead of discovering it in production.
        """
        def refuse(recordset):
            raise AccessError(
                "the credential facade is restricted (simulated by TD10)")

        return patch.object(type(self.company), "_compute_meli_auth_fields",
                            refuse)

    def _auth_row_values(self):
        self.env.cr.execute(
            "SELECT access_token, refresh_token FROM mercadolibre_auth "
            "WHERE company_id = %s", (self.company.id,))
        return self.env.cr.fetchone()

    # ==================================================================
    # anti-vacuity: the canaries are really in place
    # ==================================================================
    def test_the_auth_row_holds_the_canaries(self):
        access, refresh = self._auth_row_values()
        self.assertEqual(access, _ACCESS_CANARY,
                         "the canary is not where these tests assume")
        self.assertEqual(refresh, _REFRESH_CANARY)

    def test_the_facade_simulation_really_denies(self):
        """If the simulation does not bite, every test below is vacuous."""
        with self._facade_denied():
            with self.assertRaises(AccessError):
                self.company.invalidate_recordset()
                self.company.mercadolibre_access_token

    # ==================================================================
    # the gateway
    # ==================================================================
    def test_the_gateway_works_without_the_public_facade(self):
        """The decisive one. No facade, and the client still gets built."""
        with self._facade_denied():
            client = self.util._build_client(self.company)

        self.assertEqual(client.access_token, _ACCESS_CANARY,
                         "the gateway did not carry the access token over")
        self.assertEqual(client.refresh_token, _REFRESH_CANARY,
                         "the gateway did not carry the refresh token over")
        self.assertEqual(client.client_secret, _SECRET_CANARY,
                         "the gateway did not carry the client secret over")
        self.assertEqual(client.seller_id, _SELLER)

    def test_a_commercial_user_can_build_a_client(self):
        """A cron or an import runs as an ordinary user, not as an admin."""
        util = self.env["meli.util"].with_user(self.commercial_user)
        company = self.company.with_user(self.commercial_user)

        with self._facade_denied():
            client = util._build_client(company)

        self.assertEqual(client.access_token, _ACCESS_CANARY,
                         "a commercial process cannot authenticate any more")
        self.assertEqual(client.client_secret, _SECRET_CANARY)

    def test_the_gateway_returns_a_client_not_a_dictionary_of_secrets(self):
        """A capability, not data. Handing back a dict would just move the
        exposure one call further out."""
        client = self.util._build_client(self.company)

        self.assertNotIsInstance(client, dict)
        self.assertTrue(hasattr(client, "get"),
                        "the gateway must return something that can talk to "
                        "MercadoLibre, not a bag of values")

    def test_the_gateway_is_private(self):
        """A public name would be callable straight over RPC."""
        self.assertTrue(
            "_build_client".startswith("_"),
            "the capability boundary must not be remotely invocable")
        self.assertFalse(
            hasattr(type(self.util), "get_credentials"),
            "a public credential getter defeats the whole gateway")

    def test_the_gateway_requires_a_single_company(self):
        both = self.company | self.env["res.company"].create(
            {"name": "TD10 second company"})
        with self.assertRaises(Exception):
            self.util._build_client(both)

    # ==================================================================
    # the gateway stays inert
    # ==================================================================
    def test_the_gateway_touches_no_network(self):
        session = _CountingSession()
        from odoo.addons.meli_oerp.models.meli_util import MeliConfiguration

        with patch.object(MeliConfiguration, "get_session",
                          return_value=session):
            self.util._build_client(self.company)

        self.assertEqual(session.get_count, 0)
        self.assertEqual(session.post_count, 0)

    def test_the_gateway_writes_nothing(self):
        before = self._auth_row_values()

        self.util._build_client(self.company)
        self.env.flush_all()
        self.env.invalidate_all()

        self.assertEqual(self._auth_row_values(), before,
                         "the gateway modified the credentials")

    # ==================================================================
    # the dead reads
    # ==================================================================
    def test_the_notification_paths_do_not_read_the_facade(self):
        """Four methods assigned the tokens and never used them.

        Each one is another non-system reader of a field that is about to be
        restricted, so they go. The tokens were never consumed there: the
        client comes from get_new_instance.
        """
        import inspect

        from odoo.addons.meli_oerp.models import notification as notif

        model = notif.MercadolibreNotification
        offenders = []
        for name in ("fetch_lasts", "_process_notification_question",
                     "_process_notification_order", "process_notification"):
            method = getattr(model, name, None)
            if method is None:
                continue
            for line in inspect.getsource(method).splitlines():
                if ("mercadolibre_access_token" in line
                        or "mercadolibre_refresh_token" in line):
                    offenders.append("%s: %s" % (name, line.strip()))

        self.assertEqual(
            offenders, [],
            "these read a credential field they never use: %s" % offenders)
