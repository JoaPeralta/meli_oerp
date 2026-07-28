# -*- coding: utf-8 -*-
"""The token refresh runs in its own transaction, serialised on the auth row.

Two properties, and they are inseparable — which is why they ship together.

**Isolated.** MercadoLibre's refresh tokens are single-use: the moment the POST
succeeds, the old one is dead on their side. Today the refresh runs inside the
ambient business transaction, which has no commit boundary at all (``MeliCommit``
is ``flush_all()``, not ``cr.commit()``). So an import that fails after a refresh
rolls back to the spent token and the session is unrecoverable.

**Serialised.** Two processes that both see an expired token must produce
exactly one POST, not two, or the second spends a token the first already
rotated.

A lock alone would be worse than the problem. ``pg_advisory_xact_lock`` is
released when the transaction ends, so taking it inside the ambient transaction
holds it for the whole business operation — a ~163 s import blocks every other
refresh. The lock only works if the refresh has its own short transaction.

WHY *THIS* PRIMITIVE
--------------------
Measured against the real PostgreSQL behind this deployment, two live
connections, in ``probe_auth_lock_mvcc.py``:

    FAIL  REPEATABLE READ + advisory lock + re-read   (B observed R1, post=1)
    PASS  READ COMMITTED  + advisory lock + re-read   (B observed R2, post=0)
    PASS  REPEATABLE READ + FOR UPDATE + 40001 retry  (B observed R2, post=0)
    PASS  READ COMMITTED  + FOR UPDATE                (B observed R2, post=0)

The first line is the trap. Odoo 19 puts every cursor at REPEATABLE READ, and in
PostgreSQL that snapshot is fixed by the transaction's FIRST statement — which
the lock statement itself is. It fixes the snapshot and *then* blocks, so the
re-read after acquiring the lock cannot see what the previous holder committed.
It looks serialised and is not.

Hence: READ COMMITTED, and ``SELECT ... FOR UPDATE`` on the auth row, which locks
exactly the resource being mutated and needs no separate key space.

WHAT THESE TESTS DO AND DO NOT PROVE
------------------------------------
They pin the control flow: statement order, the isolation self-check, the two
guards, and that exactly one POST is issued. They drive the primitive with a
recording cursor, so they are deterministic and fast.

They do **not** prove the concurrency property. A test that calls a sequential
function twice with mocks cannot distinguish a working primitive from the
failing REPEATABLE READ one — both look identical single-threaded. That property
is proven by the two-connection probe above, run against real PostgreSQL, and is
re-run against this implementation before merge.
"""

from unittest.mock import patch

import psycopg2

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_SELLER = "2288636236"
_OLD_ACCESS = "OLD-ACCESS-000000000000-%s" % _SELLER
_OLD_REFRESH = "OLD-REFRESH-111111111111"
_NEW_ACCESS = "NEW-ACCESS-222222222222-%s" % _SELLER
_NEW_REFRESH = "NEW-REFRESH-333333333333"
_ROTATED_REFRESH = "ROTATED-BY-SOMEONE-ELSE-444444"
_AUTH_ROW_ID = 77


class _RecordingCursor:
    """Stands in for the AUTH transaction's own cursor.

    Records every statement in order, so the mandatory sequence can be asserted
    rather than assumed.
    """

    def __init__(self, auth_row=(_AUTH_ROW_ID, _OLD_ACCESS, _OLD_REFRESH),
                 isolation="read committed", for_update_exc=None,
                 commit_exc=None):
        self.statements = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self._auth_row = auth_row
        self._isolation = isolation
        self._for_update_exc = for_update_exc
        self._commit_exc = commit_exc
        self._result = []

    # -- the bits the primitive uses -----------------------------------
    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self.statements.append(text)
        low = text.lower()
        if "show transaction_isolation" in low:
            self._result = [(self._isolation,)]
        elif "for update" in low:
            if self._for_update_exc is not None:
                raise self._for_update_exc
            self._result = [self._auth_row] if self._auth_row else []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def commit(self):
        if self._commit_exc is not None:
            raise self._commit_exc
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- helpers for the assertions ------------------------------------
    def first_statement(self):
        return self.statements[0] if self.statements else None

    def matching(self, needle):
        return [s for s in self.statements if needle.lower() in s.lower()]


class _FakeMeliClient:
    """Counts POSTs. Never touches the network."""

    def __init__(self, response=None, exc=None):
        self.access_token = _OLD_ACCESS
        self.refresh_token = _OLD_REFRESH
        self.client_id = "1234567890123456"
        self.client_secret = "client-secret-value-4444444444"
        self.seller_id = _SELLER
        self.post_count = 0
        self.refresh_token_used = None
        self._response = response
        self._exc = exc

    def get_refresh_token(self, code=None, redirect_uri=None):
        self.post_count += 1
        self.refresh_token_used = self.refresh_token
        if self._exc is not None:
            raise self._exc
        return self._response


def _valid_response(**over):
    payload = {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH,
               "token_type": "Bearer", "expires_in": 21600,
               "user_id": int(_SELLER)}
    payload.update(over)
    return payload


@tagged("post_install", "-at_install")
class TestIsolatedSerialisedRefresh(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _OLD_ACCESS,
            "mercadolibre_refresh_token": _OLD_REFRESH,
            "mercadolibre_client_id": "1234567890123456",
            "mercadolibre_secret_key": "client-secret-value-4444444444",
        })
        self.env.flush_all()
        self.util = self.env["meli.util"]

    def _refresh(self, cursor, client):
        with patch.object(type(self.util), "_meli_auth_cursor",
                          return_value=cursor):
            return self.util._meli_refresh_credentials(self.company, client)

    # ------------------------------------------------------------------
    # the mandatory statement order
    # ------------------------------------------------------------------
    def test_the_isolation_level_is_set_before_any_other_statement(self):
        """Nothing may run before the SET.

        Under REPEATABLE READ the snapshot is fixed by the first statement of
        the transaction. Any query issued before the isolation level is changed
        would fix it, and the whole primitive would be serialising against a
        view of the row taken before it ever waited.
        """
        cursor = _RecordingCursor()

        self._refresh(cursor, _FakeMeliClient(_valid_response()))

        self.assertEqual(
            cursor.first_statement(),
            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
            "the isolation level must be set by the very first statement; "
            "issuing anything before it fixes the snapshot")
        self.assertIn(
            "show transaction_isolation", cursor.statements[1].lower(),
            "the isolation level must be read back immediately after being set")

    def test_it_refuses_to_run_when_the_isolation_level_did_not_apply(self):
        """A silent degradation reproduces the primitive that was proven to fail."""
        cursor = _RecordingCursor(isolation="repeatable read")
        client = _FakeMeliClient(_valid_response())

        result = self._refresh(cursor, client)

        self.assertEqual(result.status, "AUTH_CRITICAL")
        self.assertEqual(
            cursor.matching("for update"), [],
            "the row was locked despite the isolation level not applying")
        self.assertEqual(
            client.post_count, 0,
            "a POST was issued while running at an isolation level proven not "
            "to serialise the refresh")
        self.assertFalse(cursor.committed)

    # ------------------------------------------------------------------
    # guard: FOR UPDATE on zero rows locks nothing, silently
    # ------------------------------------------------------------------
    def test_a_missing_auth_row_is_critical_and_never_posts(self):
        """SELECT ... FOR UPDATE matching no row returns quietly and locks
        nothing. Continuing from there would be an unserialised refresh that
        looks serialised."""
        cursor = _RecordingCursor(auth_row=None)
        client = _FakeMeliClient(_valid_response())

        result = self._refresh(cursor, client)

        self.assertEqual(result.status, "AUTH_CRITICAL")
        self.assertEqual(
            client.post_count, 0,
            "a POST was issued without holding any lock at all")
        self.assertFalse(cursor.committed)
        self.assertTrue(cursor.rolled_back)

    # ------------------------------------------------------------------
    # re-evaluate: identity of the refresh token, not the clock
    # ------------------------------------------------------------------
    def test_it_does_not_post_when_another_process_already_rotated(self):
        """The waiter must observe the winner's credentials and stand down.

        Compared by identity rather than by expiry time: no dependence on clock
        skew, and it still works when MercadoLibre omitted expires_in.
        """
        cursor = _RecordingCursor(
            auth_row=(_AUTH_ROW_ID, _NEW_ACCESS, _ROTATED_REFRESH))
        client = _FakeMeliClient(_valid_response())

        result = self._refresh(cursor, client)

        self.assertEqual(result.status, "ALREADY_FRESH")
        self.assertEqual(
            client.post_count, 0,
            "posted a refresh token another process had already spent")
        self.assertEqual(result.access_token, _NEW_ACCESS)
        self.assertEqual(result.refresh_token, _ROTATED_REFRESH)

    # ------------------------------------------------------------------
    # exactly one POST, validated, persisted, committed
    # ------------------------------------------------------------------
    def test_a_valid_refresh_posts_once_persists_and_commits(self):
        cursor = _RecordingCursor()
        client = _FakeMeliClient(_valid_response())

        result = self._refresh(cursor, client)

        self.assertEqual(result.status, "REFRESHED")
        self.assertEqual(client.post_count, 1,
                         "exactly one POST per refresh")
        self.assertTrue(cursor.matching("update mercadolibre_auth"),
                        "the new credentials were never written")
        self.assertTrue(cursor.committed,
                        "the AUTH transaction did not commit, so a business "
                        "rollback would discard a rotation MercadoLibre "
                        "already performed")
        self.assertEqual(result.access_token, _NEW_ACCESS)
        self.assertEqual(result.refresh_token, _NEW_REFRESH)

    def test_the_post_uses_the_token_re_read_under_the_lock(self):
        """Not the one the caller happened to be holding."""
        cursor = _RecordingCursor(
            auth_row=(_AUTH_ROW_ID, _OLD_ACCESS, "RE-READ-REFRESH-999999"))
        client = _FakeMeliClient(_valid_response())
        client.refresh_token = "RE-READ-REFRESH-999999"

        self._refresh(cursor, client)

        self.assertEqual(client.refresh_token_used, "RE-READ-REFRESH-999999")

    def test_the_auth_transaction_never_touches_res_company(self):
        """The whole reason the auth row exists.

        An UPDATE on res_company here would abort any concurrent business
        transaction that later writes the same row.
        """
        cursor = _RecordingCursor()

        self._refresh(cursor, _FakeMeliClient(_valid_response()))

        offenders = [s for s in cursor.statements
                     if "res_company" in s.lower()]
        self.assertEqual(
            offenders, [],
            "the AUTH transaction touched res_company: %s" % offenders)

    # ------------------------------------------------------------------
    # rejected responses
    # ------------------------------------------------------------------
    def test_a_response_for_another_seller_is_not_persisted(self):
        cursor = _RecordingCursor()
        client = _FakeMeliClient(_valid_response(user_id=9999999999))

        result = self._refresh(cursor, client)

        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(
            cursor.matching("update mercadolibre_auth"), [],
            "credentials issued for a different account were persisted")
        self.assertFalse(cursor.committed)
        self.assertEqual(result.access_token, _OLD_ACCESS,
                         "the rejected credentials were handed to the caller")
        self.assertEqual(result.refresh_token, _OLD_REFRESH)

    def test_invalid_grant_aborts_for_reauth_without_touching_the_tokens(self):
        cursor = _RecordingCursor()
        client = _FakeMeliClient({"error": "invalid_grant",
                                  "message": "refresh token expired"})

        result = self._refresh(cursor, client)

        self.assertEqual(result.status, "ABORT_REAUTH")
        self.assertEqual(cursor.matching("update mercadolibre_auth"), [])
        self.assertFalse(cursor.committed)

    def test_a_transport_failure_is_uncertain_and_never_retried(self):
        """POST sent, no answer: we cannot know whether MercadoLibre spent it."""
        cursor = _RecordingCursor()
        client = _FakeMeliClient(exc=Exception("connection reset"))

        result = self._refresh(cursor, client)

        self.assertEqual(result.status, "AUTH_UNCERTAIN")
        self.assertEqual(client.post_count, 1,
                         "an ambiguous POST must never be retried")
        self.assertEqual(cursor.matching("update mercadolibre_auth"), [])
        self.assertFalse(cursor.committed)

    # ------------------------------------------------------------------
    # serialisation failure: not the normal path under READ COMMITTED
    # ------------------------------------------------------------------
    def test_a_serialization_failure_is_handled_explicitly(self):
        """READ COMMITTED does not raise 40001 on FOR UPDATE.

        If it ever arrives it means the transaction is not running where it
        believes it is, so it aborts loudly instead of continuing.
        """
        exc = psycopg2.errors.SerializationFailure(
            "could not serialize access due to concurrent update")
        cursor = _RecordingCursor(for_update_exc=exc)
        client = _FakeMeliClient(_valid_response())

        result = self._refresh(cursor, client)

        self.assertEqual(result.status, "ABORT_INDETERMINATE")
        self.assertEqual(client.post_count, 0)
        self.assertFalse(cursor.committed)
        self.assertTrue(cursor.rolled_back)
