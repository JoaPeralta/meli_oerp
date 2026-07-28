# -*- coding: utf-8 -*-
"""Test seam for driving the AUTH transaction from inside a TransactionCase.

Not a test module. Deliberately not registered in ``tests/__init__.py``.

WHY IT IS NEEDED
----------------
``_meli_refresh_credentials`` opens a real second connection. That is the whole
point in production, and it is exactly what a ``TransactionCase`` cannot
accommodate: the auth row the test just created is still uncommitted, so a
second connection cannot see it, and the primitive correctly refuses to refresh
an auth row it cannot lock.

This wrapper runs the primitive's statements on the test's own cursor, so the
full path is exercised -- statement order, the ``FOR UPDATE``, re-read,
re-evaluation, validation, the ``UPDATE`` -- inside the test transaction.

WHAT IT FAKES, AND WHY THAT IS HONEST
-------------------------------------
Two statements are intercepted rather than executed:

* ``SET TRANSACTION ISOLATION LEVEL`` -- a transaction already in flight cannot
  change its level, and the test transaction is in flight by definition.
* ``SHOW transaction_isolation`` -- answered with ``read committed`` so the
  primitive's self-check passes.

Faking the self-check here is only acceptable because it is tested for real
elsewhere: ``test_isolated_serialised_refresh`` drives the primitive with a
recording cursor and asserts both that the ``SET`` is the very first statement
issued and that a level which failed to apply refuses to refresh.

``commit()`` is recorded rather than performed, because committing would escape
the test's rollback and leave credentials behind in the CI database.

WHAT NOTHING HERE PROVES
------------------------
The isolation itself. No single-threaded test can distinguish a working
primitive from the REPEATABLE READ one that was *measured* to fail -- both look
identical when driven sequentially. That property is proven by
``probe_auth_lock_mvcc.py``, two live connections against real PostgreSQL.
"""


class AmbientAuthCursor:
    """Runs the AUTH statements on the ambient test cursor."""

    def __init__(self, cr):
        self._cr = cr
        self.statements = []
        self.committed = False
        self.rolled_back = False
        self._faked_isolation = False

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self.statements.append(text)
        upper = text.upper()
        if upper.startswith("SET TRANSACTION ISOLATION LEVEL"):
            return None
        if "SHOW TRANSACTION_ISOLATION" in upper:
            self._faked_isolation = True
            return None
        return self._cr.execute(query, params)

    def fetchone(self):
        if self._faked_isolation:
            self._faked_isolation = False
            return ("read committed",)
        return self._cr.fetchone()

    def commit(self):
        # Recorded, never performed: a real commit would survive the test's
        # rollback and leave credentials in the CI database.
        self.committed = True

    def rollback(self):
        # Not forwarded either. Rolling back the ambient cursor would discard
        # the whole test transaction, fixtures included.
        self.rolled_back = True

    def close(self):
        pass

    def posted_and_persisted(self):
        """True when the primitive got as far as writing the new credentials.

        The positive signal these tests need. Several assertions in this suite
        are of the form "the stored credentials did not change", and an
        AUTH transaction that bailed out early satisfies those too -- for
        entirely the wrong reason.
        """
        return (self.committed
                and any("UPDATE mercadolibre_auth" in s for s in self.statements))
