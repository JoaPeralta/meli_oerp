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
failing REPEATABLE READ one — both look identical single-threaded.

What the probe establishes is that the *primitive* — READ COMMITTED plus
``SELECT ... FOR UPDATE`` — serialises correctly on this PostgreSQL. What it
does not establish is that this particular implementation of it does, because
running it would require the code to be present in a live Odoo, and the deployed
container still runs a commit that predates all of TD8. Confirming this
implementation under genuine concurrency belongs to the controlled real
validation, which is blocked on deployment and is not claimed here.
"""

from unittest.mock import patch

import psycopg2
import requests

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.meli_oerp.models.meli_util import (
    MeliApiNoSDK,
    MeliTokenOutcome,
    configuration_nosdk,
)

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
                 commit_exc=None, update_exc=None, update_rowcount=1,
                 events=None, name="cursor"):
        self.statements = []
        self.params = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.rowcount = 0
        self._auth_row = auth_row
        self._isolation = isolation
        self._for_update_exc = for_update_exc
        self._commit_exc = commit_exc
        self._update_exc = update_exc
        self._update_rowcount = update_rowcount
        self._result = []
        # Shared ordering log, so tests can assert that the original
        # transaction was released before recovery opened.
        self._events = events if events is not None else []
        self._name = name

    def _note(self, what):
        self._events.append("%s:%s" % (self._name, what))

    # -- the bits the primitive uses -----------------------------------
    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self.statements.append(text)
        self.params.append(params)
        self._note("execute")
        low = text.lower()
        if "show transaction_isolation" in low:
            self._result = [(self._isolation,)]
        elif "for update" in low:
            if self._for_update_exc is not None:
                raise self._for_update_exc
            self._result = [self._auth_row] if self._auth_row else []
        elif "update mercadolibre_auth" in low:
            if self._update_exc is not None:
                raise self._update_exc
            self.rowcount = self._update_rowcount
            self._result = []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def commit(self):
        if self._commit_exc is not None:
            raise self._commit_exc
        self.committed = True
        self._note("commit")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True
        self._note("close")

    def params_for(self, needle):
        """Parameters of the first statement matching `needle`."""
        for text, params in zip(self.statements, self.params):
            if needle.lower() in text.lower():
                return params
        return None

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
    """Counts POSTs. Never touches the network.

    Returns a ``MeliTokenOutcome``, the contract both backends now honour, so
    these tests exercise the same shape the real client produces.
    """

    def __init__(self, outcome=None, exc=None):
        self.access_token = _OLD_ACCESS
        self.refresh_token = _OLD_REFRESH
        self.client_id = "1234567890123456"
        self.client_secret = "client-secret-value-4444444444"
        self.seller_id = _SELLER
        self.post_count = 0
        self.refresh_token_used = None
        self._outcome = outcome
        self._exc = exc

    def get_refresh_token(self, code=None, redirect_uri=None):
        self.post_count += 1
        self.refresh_token_used = self.refresh_token
        if self._exc is not None:
            raise self._exc
        return self._outcome


def _valid_payload(**over):
    payload = {"access_token": _NEW_ACCESS, "refresh_token": _NEW_REFRESH,
               "token_type": "Bearer", "expires_in": 21600,
               "user_id": int(_SELLER)}
    payload.update(over)
    return payload


def _valid_response(**over):
    return MeliTokenOutcome(payload=_valid_payload(**over), http_status=200)


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _PostSession:
    """Real requests.Session stand-in: counts attempts, can raise or answer."""

    def __init__(self, status=200, payload=None, exc=None):
        self.attempts = 0
        self._status = status
        self._payload = payload if payload is not None else _valid_payload()
        self._exc = exc

    def post(self, url, **kwargs):
        self.attempts += 1
        if self._exc is not None:
            raise self._exc
        return _Resp(self._status, self._payload)


def _real_nosdk_client(session):
    """The actual MeliApiNoSDK, driven through a fake socket layer.

    The point of using the real backend: it catches requests.RequestException
    itself, so a transport failure never surfaces as a Python exception to the
    caller. A test that raises straight out of a fake client is testing a
    situation the production code cannot produce.
    """
    client = MeliApiNoSDK(config=configuration_nosdk)
    client._session = session
    client.access_token = _OLD_ACCESS
    client.refresh_token = _OLD_REFRESH
    client.client_id = "1234567890123456"
    client.client_secret = "client-secret-value-4444444444"
    client.seller_id = _SELLER
    return client


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
        client = _FakeMeliClient(MeliTokenOutcome(
            payload={"error": "invalid_grant", "message": "refresh token expired"},
            http_status=400))

        result = self._refresh(cursor, client)

        self.assertEqual(result.status, "ABORT_REAUTH")
        self.assertEqual(cursor.matching("update mercadolibre_auth"), [])
        self.assertFalse(cursor.committed)

    # ------------------------------------------------------------------
    # the locked row is the only authority on which token to post
    # ------------------------------------------------------------------
    def test_an_empty_stored_refresh_token_never_posts(self):
        """The caller's token is a generation marker, never a fallback.

        Falling back to it would post a token the locked row does not vouch
        for -- exactly the unserialised behaviour the lock exists to prevent.
        """
        cursor = _RecordingCursor(auth_row=(_AUTH_ROW_ID, _OLD_ACCESS, ""))
        client = _FakeMeliClient(_valid_response())
        client.refresh_token = _OLD_REFRESH

        result = self._refresh(cursor, client)

        self.assertEqual(
            client.post_count, 0,
            "posted the caller's refresh token even though the locked row "
            "carries none")
        self.assertIn(result.status, ("ABORT_REAUTH", "AUTH_CRITICAL"))
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


@tagged("post_install", "-at_install")
class TestRefreshErrorPathsThroughTheRealBackend(TransactionCase):
    """The error paths, driven through ``MeliApiNoSDK`` rather than around it.

    This class exists because the first version of these tests was wrong. It
    used a fake client that raised ``Exception`` from ``get_refresh_token`` and
    asserted the primitive called that ``AUTH_UNCERTAIN``. The real backend
    never does that: it catches ``requests.RequestException`` itself and returns
    an error payload, so a genuine timeout reached the primitive looking like
    any other bad response and was classified ``REJECTED``.

    A refresh that timed out and one MercadoLibre refused are not the same
    event. After a timeout we cannot know whether the refresh token was spent,
    so nothing may be retried and nothing may be written. Getting that wrong is
    how a single-use token gets burned twice.
    """

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

    def _assert_nothing_was_written(self, cursor, result):
        self.assertEqual(
            cursor.matching("update mercadolibre_auth"), [],
            "credentials were written on a path that must not write")
        self.assertFalse(cursor.committed)
        self.assertEqual(result.access_token, _OLD_ACCESS)
        self.assertEqual(result.refresh_token, _OLD_REFRESH)

    # -- transport uncertainty -----------------------------------------
    def test_a_real_timeout_is_uncertain_and_never_retried(self):
        session = _PostSession(exc=requests.Timeout("timed out"))
        cursor = _RecordingCursor()

        result = self._refresh(cursor, _real_nosdk_client(session))

        self.assertEqual(
            session.attempts, 1,
            "the POST was attempted more than once after an ambiguous "
            "timeout; MercadoLibre may already have spent the token")
        self.assertEqual(
            result.status, "AUTH_UNCERTAIN",
            "a timeout was classified as a refusal. We cannot know whether "
            "the refresh token was consumed, and calling it a refusal invites "
            "a retry that would spend a second one")
        self._assert_nothing_was_written(cursor, result)

    def test_a_real_connection_error_is_uncertain(self):
        session = _PostSession(exc=requests.ConnectionError("reset by peer"))
        cursor = _RecordingCursor()

        result = self._refresh(cursor, _real_nosdk_client(session))

        self.assertEqual(session.attempts, 1)
        self.assertEqual(result.status, "AUTH_UNCERTAIN")
        self._assert_nothing_was_written(cursor, result)

    # -- classified by HTTP status, not by the body --------------------
    def _assert_indeterminate(self, status_code):
        session = _PostSession(status=status_code,
                               payload={"message": "not about the token"})
        cursor = _RecordingCursor()

        result = self._refresh(cursor, _real_nosdk_client(session))

        self.assertEqual(
            result.status, "ABORT_INDETERMINATE",
            "HTTP %s says nothing about whether the token is still valid; "
            "treating it as expired triggers a refresh that is not needed"
            % status_code)
        self.assertEqual(session.attempts, 1,
                         "HTTP %s was retried" % status_code)
        self._assert_nothing_was_written(cursor, result)

    def test_http_403_is_indeterminate(self):
        self._assert_indeterminate(403)

    def test_http_429_is_indeterminate(self):
        self._assert_indeterminate(429)

    def test_http_500_is_indeterminate(self):
        self._assert_indeterminate(500)

    def test_http_503_is_indeterminate(self):
        self._assert_indeterminate(503)

    def test_invalid_grant_is_still_reauth_not_indeterminate(self):
        """The one refusal that really is about the token."""
        session = _PostSession(
            status=400,
            payload={"error": "invalid_grant", "message": "expired"})
        cursor = _RecordingCursor()

        result = self._refresh(cursor, _real_nosdk_client(session))

        self.assertEqual(result.status, "ABORT_REAUTH")
        self._assert_nothing_was_written(cursor, result)

    def test_a_valid_response_through_the_real_backend_is_accepted(self):
        """Guard: the classifications above mean nothing if nothing succeeds."""
        session = _PostSession(status=200, payload=_valid_payload())
        cursor = _RecordingCursor()

        result = self._refresh(cursor, _real_nosdk_client(session))

        self.assertEqual(result.status, "REFRESHED")
        self.assertTrue(cursor.matching("update mercadolibre_auth"))
        self.assertTrue(cursor.committed)


@tagged("post_install", "-at_install")
class TestRecoveryAfterAValidRotation(TransactionCase):
    """Once MercadoLibre has rotated the token, losing it locally is fatal.

    R1 is spent the instant the POST succeeds. If the local UPDATE or COMMIT
    then fails, A2/R2 exist only in this process's memory, and they are the only
    credentials that can still reach MercadoLibre. Re-posting is not a recovery:
    it would spend R2 as well.

    So everything after a valid response -- deriving the expiry, the UPDATE, the
    COMMIT -- is one protected phase. Any failure inside it releases the
    original transaction first, then retries the persist alone, under exclusion,
    with no second POST.

    Recovery must also be able to tell what it is looking at:

        row already holds R2      -> the original commit did land -> success
        row still holds R1        -> persist exactly A2/R2
        row holds something else  -> a third generation exists; do not
                                     overwrite it -> AUTH_CRITICAL
        no row, or rowcount != 1  -> AUTH_CRITICAL
    """

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
        self.events = []

    def _refresh(self, first, second, client):
        handed = []

        def _next(*args, **kwargs):
            cur = first if not handed else second
            handed.append(cur)
            return cur

        with patch.object(type(self.util), "_meli_auth_cursor",
                          side_effect=_next):
            return self.util._meli_refresh_credentials(self.company, client)

    def _primary(self, **kw):
        kw.setdefault("events", self.events)
        kw.setdefault("name", "primary")
        return _RecordingCursor(**kw)

    def _recovery(self, **kw):
        kw.setdefault("events", self.events)
        kw.setdefault("name", "recovery")
        return _RecordingCursor(**kw)

    # ------------------------------------------------------------------
    def test_a_failed_update_recovers_without_a_second_post(self):
        first = self._primary(update_exc=RuntimeError("disk full"))
        second = self._recovery(
            auth_row=(_AUTH_ROW_ID, _OLD_ACCESS, _OLD_REFRESH))
        client = _FakeMeliClient(_valid_response())

        result = self._refresh(first, second, client)

        self.assertEqual(client.post_count, 1,
                         "recovery issued a second POST, spending R2 as well")
        # Positive guard: recovery must have WRITTEN A2/R2, not merely
        # reported success.
        self.assertTrue(second.matching("update mercadolibre_auth"),
                        "recovery never wrote the rotated credentials")
        self.assertIn(
            _NEW_REFRESH, list(second.params_for("update mercadolibre_auth")),
            "recovery wrote something other than the rotated refresh token")
        self.assertTrue(second.committed)
        self.assertEqual(result.status, "REFRESHED")
        self.assertEqual(result.refresh_token, _NEW_REFRESH)

    def test_a_failed_commit_recovers_without_a_second_post(self):
        first = self._primary(commit_exc=RuntimeError("connection lost"))
        second = self._recovery(
            auth_row=(_AUTH_ROW_ID, _OLD_ACCESS, _OLD_REFRESH))
        client = _FakeMeliClient(_valid_response())

        result = self._refresh(first, second, client)

        self.assertEqual(client.post_count, 1)
        self.assertTrue(second.matching("update mercadolibre_auth"))
        self.assertIn(
            _NEW_REFRESH, list(second.params_for("update mercadolibre_auth")))
        self.assertTrue(second.committed)
        self.assertEqual(result.status, "REFRESHED")

    def test_the_original_transaction_is_released_before_recovery_starts(self):
        """Otherwise recovery waits on a FOR UPDATE the failed transaction may
        still be holding, and blocks against itself."""
        first = self._primary(commit_exc=RuntimeError("connection lost"))
        second = self._recovery(
            auth_row=(_AUTH_ROW_ID, _OLD_ACCESS, _OLD_REFRESH))

        self._refresh(first, second, _FakeMeliClient(_valid_response()))

        self.assertIn("recovery:execute", self.events,
                      "recovery never ran, so the ordering assertion below "
                      "would be vacuous")
        self.assertIn("primary:close", self.events,
                      "the original cursor was never released")
        self.assertLess(
            self.events.index("primary:close"),
            self.events.index("recovery:execute"),
            "recovery opened while the original transaction could still hold "
            "the FOR UPDATE on the same row")

    def test_recovery_treats_an_already_rotated_row_as_success(self):
        """The original commit may have landed before the failure was seen."""
        first = self._primary(commit_exc=RuntimeError("connection lost"))
        second = self._recovery(
            auth_row=(_AUTH_ROW_ID, _NEW_ACCESS, _NEW_REFRESH))

        result = self._refresh(first, second, _FakeMeliClient(_valid_response()))

        self.assertEqual(result.status, "REFRESHED")
        self.assertEqual(
            second.matching("update mercadolibre_auth"), [],
            "recovery rewrote a row that already held the rotated credentials")

    def test_recovery_refuses_to_overwrite_a_third_generation(self):
        first = self._primary(commit_exc=RuntimeError("connection lost"))
        second = self._recovery(
            auth_row=(_AUTH_ROW_ID, "THIRD-ACCESS-888888", "THIRD-REFRESH-888888"))

        result = self._refresh(first, second, _FakeMeliClient(_valid_response()))

        self.assertEqual(result.status, "AUTH_CRITICAL")
        self.assertEqual(
            second.matching("update mercadolibre_auth"), [],
            "recovery overwrote credentials a third process had rotated")

    def test_recovery_that_matches_no_row_is_critical(self):
        first = self._primary(commit_exc=RuntimeError("connection lost"))
        second = self._recovery(auth_row=None)

        result = self._refresh(first, second, _FakeMeliClient(_valid_response()))

        self.assertEqual(result.status, "AUTH_CRITICAL")
        self.assertFalse(second.committed)

    def test_recovery_that_updates_zero_rows_is_critical(self):
        first = self._primary(commit_exc=RuntimeError("connection lost"))
        second = self._recovery(
            auth_row=(_AUTH_ROW_ID, _OLD_ACCESS, _OLD_REFRESH),
            update_rowcount=0)

        result = self._refresh(first, second, _FakeMeliClient(_valid_response()))

        self.assertEqual(result.status, "AUTH_CRITICAL")
        self.assertFalse(second.committed)

    def test_a_failing_recovery_is_critical_and_still_never_posts_twice(self):
        first = self._primary(commit_exc=RuntimeError("connection lost"))
        second = self._recovery(
            auth_row=(_AUTH_ROW_ID, _OLD_ACCESS, _OLD_REFRESH),
            commit_exc=RuntimeError("still down"))
        client = _FakeMeliClient(_valid_response())

        result = self._refresh(first, second, client)

        self.assertEqual(result.status, "AUTH_CRITICAL")
        self.assertEqual(client.post_count, 1,
                         "a failing recovery must never fall back to posting")
