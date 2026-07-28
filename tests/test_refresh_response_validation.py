# -*- coding: utf-8 -*-
"""A refresh response must be validated before its credentials are trusted.

Today the only check is ``if "access_token" in refjson``. Everything else is
taken on faith:

    api_rest_client.access_token  = refjson["access_token"]
    api_rest_client.refresh_token = refjson["refresh_token"]   # KeyError if absent

Three ways that goes wrong:

* **``refresh_token`` missing.** ``get_refresh_token`` already did
  ``self.refresh_token = response_info.get('refresh_token', '')`` — it replaced
  the working refresh token with an **empty string**. The next renewal then has
  nothing to send, and since MercadoLibre's refresh tokens are single-use the
  previous one is already spent. The session is unrecoverable without a manual
  OAuth round.

* **``user_id`` not checked.** A response for a different account would be
  accepted and stored as this company's credentials.

* **empty strings.** ``"access_token" in refjson`` is true for
  ``{"access_token": ""}``.

The rule adopted here: a refresh is successful only if the response carries a
non-empty ``access_token``, a non-empty ``refresh_token``, and a ``user_id``
matching the configured seller. Anything else leaves the stored credentials
untouched — a bad refresh must not be able to destroy a working session.

Validating is not enough on its own. ``get_refresh_token`` mutates the client
**before** the caller gets a chance to validate:

    # get_refresh_token()  -- runs first
    self.access_token = response_info["access_token"]

    # get_new_instance()   -- runs after
    meli_validate_refresh_response(...)

So a rejected response still reaches the caller: the credentials are correctly
kept out of the database, but the client object returned by ``get_new_instance``
is already carrying them. "Not persisted" and "not used" are two different
properties, and only the first one was covered. Every request made with that
returned client would be sent with credentials we just decided not to trust.

The rule this file pins down: a rejected refresh leaves the returned client on
the credentials that were there before the attempt.

Scope: validating the response, and not trusting it in memory either.
Serialising concurrent refreshes and the transaction boundary are separate
changes.
"""

import ast
import inspect
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.meli_oerp.models import meli_util as meli_util_module
from odoo.addons.meli_oerp.models.meli_util import MeliConfiguration
from odoo.addons.meli_oerp.tests.meli_auth_test_cursor import AmbientAuthCursor

_SELLER = "2288636236"
_OTHER_SELLER = "9999999999"
_OLD_ACCESS = "OLD-ACCESS-value-000000000000-%s" % _SELLER
_OLD_REFRESH = "OLD-REFRESH-value-111111111111"
_NEW_ACCESS = "NEW-ACCESS-value-222222222222-%s" % _SELLER
_NEW_REFRESH = "NEW-REFRESH-value-333333333333"


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, post_payload):
        self.post_payload = post_payload

    def get(self, url, **kwargs):
        return _Resp(401, {"error": "invalid_token",
                           "message": "invalid or expired token", "status": 401})

    def post(self, url, **kwargs):
        return _Resp(200, self.post_payload)

    def put(self, url, **kwargs):
        return _Resp(200, {})

    def delete(self, url, **kwargs):
        return _Resp(200, {})


@tagged("post_install", "-at_install")
class TestRefreshResponseValidation(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _OLD_ACCESS,
            "mercadolibre_refresh_token": _OLD_REFRESH,
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": "client-secret-value-4444444444",
            "mercadolibre_cron_refresh": True,
        })

    def _refresh_with(self, payload):
        session = _Session(payload)
        # The auth row must be visible to the FOR UPDATE the primitive issues.
        self.env.flush_all()
        self.auth_cr = AmbientAuthCursor(self.env.cr)
        with patch.object(MeliConfiguration, "get_session", return_value=session),                 patch.object(type(self.env["meli.util"]), "_meli_auth_cursor",
                             return_value=self.auth_cr):
            client = self.env["meli.util"].get_new_instance(self.company)
        # The AUTH transaction persists with raw SQL, so the ORM cache is stale.
        self.env.invalidate_all()
        return client

    def _assert_refresh_actually_ran(self):
        """Positive signal before any "nothing changed" assertion.

        An AUTH transaction that bails out early -- no auth row, wrong
        isolation level -- also leaves the credentials untouched. Without this,
        those assertions would pass for entirely the wrong reason.
        """
        self.assertTrue(
            any("FOR UPDATE" in s.upper() for s in self.auth_cr.statements),
            "the AUTH transaction never locked the auth row, so it never got "
            "as far as judging the response")

    def _good(self, **over):
        p = {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH,
             "token_type": "Bearer", "expires_in": 21600, "user_id": int(_SELLER)}
        p.update(over)
        return p

    def _assert_credentials_untouched(self, why):
        self._assert_refresh_actually_ran()
        self.assertEqual(self.company.mercadolibre_access_token, _OLD_ACCESS, why)
        self.assertEqual(self.company.mercadolibre_refresh_token, _OLD_REFRESH, why)

    def _assert_client_untouched(self, client, why):
        """The returned client must not carry credentials we refused to store."""
        self.assertEqual(client.access_token, _OLD_ACCESS, why)
        self.assertEqual(client.refresh_token, _OLD_REFRESH, why)

    # ------------------------------------------------------------------
    # the happy path still works
    # ------------------------------------------------------------------
    def test_a_valid_response_is_accepted(self):
        self._refresh_with(self._good())

        self.assertEqual(self.company.mercadolibre_access_token, _NEW_ACCESS)
        self.assertEqual(self.company.mercadolibre_refresh_token, _NEW_REFRESH)

    # ------------------------------------------------------------------
    # incomplete responses
    # ------------------------------------------------------------------
    def test_response_without_refresh_token_is_rejected(self):
        """The dangerous one: it used to blank the working refresh token."""
        payload = self._good()
        del payload["refresh_token"]

        self._refresh_with(payload)

        self._assert_credentials_untouched(
            "a response without refresh_token overwrote the stored credentials; "
            "the previous refresh token is single-use and already spent")
        self.assertTrue(
            self.company.mercadolibre_refresh_token,
            "the refresh token was blanked: the session cannot be renewed again")

    def test_response_with_empty_refresh_token_is_rejected(self):
        self._refresh_with(self._good(refresh_token=""))
        self._assert_credentials_untouched("an empty refresh_token was accepted")

    def test_response_with_empty_access_token_is_rejected(self):
        self._refresh_with(self._good(access_token=""))
        self._assert_credentials_untouched("an empty access_token was accepted")

    # ------------------------------------------------------------------
    # wrong account
    # ------------------------------------------------------------------
    def test_response_for_another_seller_is_rejected(self):
        """Credentials belonging to a different account must never be stored."""
        self._refresh_with(self._good(user_id=int(_OTHER_SELLER)))

        self._assert_credentials_untouched(
            "credentials issued for a different MercadoLibre account were "
            "stored as this company's")

    def test_response_without_user_id_is_rejected(self):
        payload = self._good()
        del payload["user_id"]

        self._refresh_with(payload)

        self._assert_credentials_untouched(
            "a response with no user_id was accepted without verifying the "
            "account it belongs to")

    # ------------------------------------------------------------------
    # error responses
    # ------------------------------------------------------------------
    def test_invalid_grant_leaves_credentials_alone(self):
        self._refresh_with({"error": "invalid_grant",
                            "message": "refresh token expired"})
        self._assert_credentials_untouched("invalid_grant modified the credentials")

    def test_rejected_refresh_does_not_stamp_expiry_metadata(self):
        """A rejected refresh must not look like a fresh one."""
        payload = self._good()
        del payload["refresh_token"]

        self._refresh_with(payload)

        self.assertFalse(
            self.company.mercadolibre_token_refreshed_at,
            "a rejected refresh stamped the expiry metadata as if it had worked")

    # ------------------------------------------------------------------
    # rejected != unused: the returned client must not carry the rejected
    # credentials either. Keeping them out of the database is only half the
    # property; get_new_instance hands this object to the caller, and every
    # request it makes would travel with credentials we refused to trust.
    # ------------------------------------------------------------------
    def test_client_after_wrong_seller_keeps_the_old_credentials(self):
        client = self._refresh_with(self._good(user_id=int(_OTHER_SELLER)))

        self._assert_client_untouched(
            client,
            "the returned client is using credentials issued for a different "
            "MercadoLibre account; the refresh was rejected but the client was "
            "mutated before the validation ran")

    def test_client_after_missing_user_id_keeps_the_old_credentials(self):
        payload = self._good()
        del payload["user_id"]

        client = self._refresh_with(payload)

        self._assert_client_untouched(
            client,
            "the returned client is using credentials from a response whose "
            "owning account was never verified")

    def test_client_after_missing_refresh_token_keeps_the_old_credentials(self):
        payload = self._good()
        del payload["refresh_token"]

        client = self._refresh_with(payload)

        self._assert_client_untouched(
            client,
            "the returned client took the access token from a refresh that was "
            "rejected for having no refresh token")

    def test_client_after_empty_access_token_keeps_the_old_credentials(self):
        client = self._refresh_with(self._good(access_token=""))

        self._assert_client_untouched(
            client,
            "the returned client lost its working credentials to a refresh "
            "that carried an empty access token")

    def test_client_after_invalid_grant_keeps_the_old_credentials(self):
        client = self._refresh_with({"error": "invalid_grant",
                                     "message": "refresh token expired"})

        self._assert_client_untouched(
            client, "an invalid_grant response reached the returned client")

    def test_client_after_a_valid_refresh_uses_the_new_credentials(self):
        """The counterpart: refusing bad credentials must not refuse good ones.

        Without this, "never mutate the client" would pass trivially and the
        connector would keep using an expired access token forever.
        """
        client = self._refresh_with(self._good())

        self.assertEqual(client.access_token, _NEW_ACCESS,
                         "an accepted refresh did not reach the returned client")
        self.assertEqual(client.refresh_token, _NEW_REFRESH,
                         "an accepted refresh did not reach the returned client")


@tagged("post_install", "-at_install")
class TestRefreshTokenHasNoCredentialSideEffect(TransactionCase):
    """Both HTTP backends must leave credential assignment to their caller.

    The tests above drive the NoSDK backend, the only one CI can execute:
    ``USE_MELI_SDK`` is False and the optional ``meli`` package is not
    installed, so ``MeliApiSDK`` is ``None`` and its ``get_refresh_token``
    cannot even be imported, let alone called. Skipping it would leave the SDK
    copy of the same defect uncovered, so it is checked at the source level
    instead — that part of the file is parseable whether or not the package
    that would make the class exist is installed.

    The single functional caller is ``get_new_instance``, which already assigns
    the credentials itself once the response is validated (audited: the only
    other references to ``get_refresh_token`` are this test suite and
    ``melisdk/meli.py``, whose every import is commented out).
    """

    _CREDENTIAL_ATTRS = ("access_token", "refresh_token")

    def _refresh_token_definitions(self):
        source_file = inspect.getsourcefile(meli_util_module)
        with open(source_file, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=source_file)
        return [node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name == "get_refresh_token"]

    def test_both_backends_are_reachable_by_this_test(self):
        """Guard against a vacuous pass.

        A zero count of mutations only means something once the definitions
        under test have actually been found. If a rename or a refactor makes
        this list empty, every assertion below would pass while checking
        nothing at all.
        """
        self.assertEqual(
            len(self._refresh_token_definitions()), 2,
            "expected exactly two get_refresh_token implementations in "
            "models/meli_util.py (NoSDK and SDK); the source-level check below "
            "is only meaningful if both were found")

    def test_no_backend_mutates_credentials_before_validation(self):
        offenders = []
        for definition in self._refresh_token_definitions():
            for node in ast.walk(definition):
                if not isinstance(node, (ast.Assign, ast.AugAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                            and target.attr in self._CREDENTIAL_ATTRS):
                        offenders.append("line %d: self.%s"
                                         % (target.lineno, target.attr))

        self.assertEqual(
            offenders, [],
            "get_refresh_token assigns credentials onto the client before any "
            "validation can run, so a rejected response still reaches the "
            "caller in memory. The POST belongs to the backend; deciding "
            "whether to trust the answer belongs to get_new_instance. "
            "Offending assignments: %s" % ", ".join(offenders))
