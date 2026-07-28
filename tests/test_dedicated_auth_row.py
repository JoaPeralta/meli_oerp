# -*- coding: utf-8 -*-
"""MercadoLibre's mutable authentication state lives in its own row.

Why this exists, in one paragraph. Odoo 19 puts every cursor at REPEATABLE READ
(``sql_db.py``, ``Cursor.__init__``, unconditional). A refresh that commits in
its own transaction therefore collides with the ambient business transaction in
two ways: the ambient snapshot can never see the new credentials, and — the
damaging one — any later ``UPDATE`` of that same ``res_company`` row by the
ambient transaction aborts with ``could not serialize access due to concurrent
update``, taking a multi-minute import down with it. Real ambient writers to
``res.company`` exist on the MercadoLibre paths, and ``get_new_instance`` is
called dozens of times per run.

Auditing every present and future ``write()`` on ``res.company`` forever is not
a safety property. Moving the state AUTH must mutate onto its own row is:

    BUSINESS  ->  res.company / products / postings
    AUTH      ->  mercadolibre.auth

Then the two domains cannot contend, by construction rather than by vigilance.

Scope: this change moves the state and keeps behaviour identical. The isolated
READ COMMITTED transaction and ``SELECT ... FOR UPDATE`` that use the row are
the next change.

``mercadolibre_code`` travels with the tokens deliberately: the refresh path
writes it in the same ``write()`` as the credentials, so leaving it behind
would force AUTH to touch ``res_company`` and reintroduce exactly the conflict
being removed. It has no Python readers, so moving it costs nothing.
"""

import psycopg2

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

_SELLER = "2288636236"
_ACCESS = "ACCESS-value-000000000000-%s" % _SELLER
_REFRESH = "REFRESH-value-111111111111"
_NEW_ACCESS = "NEW-ACCESS-value-222222222222-%s" % _SELLER

_CREDENTIAL_FIELDS = (
    "mercadolibre_access_token",
    "mercadolibre_refresh_token",
    "mercadolibre_code",
    "mercadolibre_token_expires_in",
    "mercadolibre_token_refreshed_at",
    "mercadolibre_token_expires_at",
)

# Seed values for the collateral-damage tests below. All six non-empty, so
# that any field silently rewritten to False or 0 is unmistakable.
_SEED_CODE = "CODE-value-555555555555"
_SEED_EXPIRES_IN = 21600
_SEED_REFRESHED_AT = "2026-07-01 10:00:00"
_SEED_EXPIRES_AT = "2026-07-01 16:00:00"

_AUTH_COLUMNS = (
    "access_token",
    "refresh_token",
    "code",
    "token_expires_in",
    "token_refreshed_at",
    "token_expires_at",
)

# facade field -> the mercadolibre_auth column it stands for
_FACADE_TO_AUTH = {
    "mercadolibre_access_token": "access_token",
    "mercadolibre_refresh_token": "refresh_token",
    "mercadolibre_code": "code",
    "mercadolibre_token_expires_in": "token_expires_in",
    "mercadolibre_token_refreshed_at": "token_refreshed_at",
    "mercadolibre_token_expires_at": "token_expires_at",
}


@tagged("post_install", "-at_install")
class TestDedicatedAuthRow(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({
            "mercadolibre_seller_id": _SELLER,
            "mercadolibre_access_token": _ACCESS,
            "mercadolibre_refresh_token": _REFRESH,
        })
        self.env.flush_all()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _sql_one(self, query, *args):
        self.env.cr.execute(query, args)
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _res_company_xmin(self, company_id):
        """The transaction id that last wrote this res_company row.

        Comparing it before and after an operation answers one question
        exactly: did anything UPDATE res_company in between?
        """
        return self._sql_one(
            "SELECT xmin::text FROM res_company WHERE id = %s", company_id)

    # ------------------------------------------------------------------
    # the row itself
    # ------------------------------------------------------------------
    def test_the_company_has_exactly_one_auth_row(self):
        rows = self.env["mercadolibre.auth"].sudo().search(
            [("company_id", "=", self.company.id)])

        self.assertEqual(
            len(rows), 1,
            "a company must resolve to exactly one MercadoLibre auth row")

    def test_a_second_auth_row_for_the_same_company_is_rejected(self):
        """One row per company, enforced by the database and not by convention."""
        with self.assertRaises(psycopg2.IntegrityError), \
                mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self.env["mercadolibre.auth"].sudo().create(
                {"company_id": self.company.id})
            self.env.flush_all()

    def test_the_credentials_live_in_the_auth_row(self):
        stored = self._sql_one(
            "SELECT access_token FROM mercadolibre_auth WHERE company_id = %s",
            self.company.id)

        self.assertEqual(stored, _ACCESS,
                         "the credentials were not stored in mercadolibre_auth")

    # ------------------------------------------------------------------
    # the res.company facade
    # ------------------------------------------------------------------
    def test_credential_fields_are_not_stored_on_res_company(self):
        """A stored field would recreate the column, and the conflict with it.

        This is the invariant the whole change rests on. If someone later makes
        one of these ``store=True`` for convenience, AUTH starts writing
        ``res_company`` again and long imports start aborting with a
        serialization failure that looks nothing like its cause.
        """
        offenders = []
        for name in _CREDENTIAL_FIELDS:
            field = self.env["res.company"]._fields.get(name)
            if field is None:
                offenders.append("%s: missing" % name)
            elif field.store:
                offenders.append("%s: store=True" % name)

        self.assertEqual(
            offenders, [],
            "credential fields must be non-stored on res.company: %s"
            % ", ".join(offenders))

    def test_reading_through_the_facade_returns_the_auth_row_value(self):
        self.env.cr.execute(
            "UPDATE mercadolibre_auth SET access_token = %s WHERE company_id = %s",
            (_NEW_ACCESS, self.company.id))
        # invalidate_all and not the company's recordset: the raw UPDATE went
        # around the ORM, so it is the mercadolibre.auth cache that is stale.
        # Clearing only res.company would re-run the compute against the same
        # cached auth record and read the old value back.
        self.env.invalidate_all()

        self.assertEqual(self.company.mercadolibre_access_token, _NEW_ACCESS)

    def test_writing_the_facade_updates_the_auth_row_and_never_res_company(self):
        """The guard the whole design depends on.

        Reads the positive signal first: unless the value actually reached
        ``mercadolibre_auth``, an unchanged ``res_company`` proves nothing --
        a write that silently did nothing would satisfy it too.
        """
        xmin_before = self._res_company_xmin(self.company.id)

        self.company.mercadolibre_access_token = _NEW_ACCESS
        self.env.flush_all()

        stored = self._sql_one(
            "SELECT access_token FROM mercadolibre_auth WHERE company_id = %s",
            self.company.id)
        self.assertEqual(
            stored, _NEW_ACCESS,
            "the write never reached mercadolibre_auth, so the res_company "
            "assertion below would be vacuous")

        xmin_after = self._res_company_xmin(self.company.id)
        self.assertEqual(
            xmin_before, xmin_after,
            "writing a credential through res.company issued an UPDATE on "
            "res_company; that is the write that makes an isolated AUTH "
            "transaction abort concurrent business transactions")

    def test_writing_every_credential_field_leaves_res_company_untouched(self):
        xmin_before = self._res_company_xmin(self.company.id)

        self.company.write({
            "mercadolibre_access_token": _NEW_ACCESS,
            "mercadolibre_refresh_token": "ANOTHER-REFRESH-333333333333",
            "mercadolibre_code": "",
            "mercadolibre_token_expires_in": 21600,
        })
        self.env.flush_all()

        stored = self._sql_one(
            "SELECT refresh_token FROM mercadolibre_auth WHERE company_id = %s",
            self.company.id)
        self.assertEqual(stored, "ANOTHER-REFRESH-333333333333",
                         "the multi-field write never reached mercadolibre_auth")

        self.assertEqual(
            self._res_company_xmin(self.company.id), xmin_before,
            "a multi-field credential write touched res_company")

    # ------------------------------------------------------------------
    # access control: this table holds secrets
    # ------------------------------------------------------------------
    def _plain_internal_user(self, login):
        """An employee with base.group_user and nothing else.

        Odoo 19 renamed res.users.groups_id to group_ids.
        """
        return self.env["res.users"].create({
            "name": "Plain Employee",
            "login": login,
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
        })

    def test_a_plain_internal_user_cannot_read_the_auth_rows(self):
        """Credentials must not be enumerable by any logged-in employee."""
        user = self._plain_internal_user("plain.employee.meli.auth.test")

        with self.assertRaises(AccessError):
            self.env["mercadolibre.auth"].with_user(user).search([])

    def test_a_plain_internal_user_cannot_write_the_auth_rows(self):
        user = self._plain_internal_user("plain.employee2.meli.auth.test")
        row = self.env["mercadolibre.auth"].sudo().search(
            [("company_id", "=", self.company.id)], limit=1)

        with self.assertRaises(AccessError):
            row.with_user(user).write({"access_token": "STOLEN"})


@tagged("post_install", "-at_install")
class TestFacadeWritesOnlyTheTargetedField(TransactionCase):
    """Writing one credential must not take the other five with it.

    Odoo protects every field that declares the inverse being run. A protected
    computed field that is not already in cache does not get computed -- it
    reads as False. So an inverse shared by six fields, which reads all six and
    writes them back, is only safe while the cache happens to be warm:

        auth row: A1 / R1 / code / expiry all set
        cold cache
          -> company.write({'mercadolibre_access_token': A2})
          -> shared inverse runs
          -> access_token is in cache (it was just written) -> A2
          -> the other five are not in cache and are protected -> False
          -> row.write() of all six
          -> R1, the code and the whole expiry metadata are destroyed

    Destroying R1 is unrecoverable: MercadoLibre refresh tokens are single-use,
    so the previous one is already spent and there is no way back without a
    manual OAuth round.

    Every test here forces the cache cold on purpose -- invalidate_all plus a
    fresh browse -- because a warm cache hides the defect completely. That is
    exactly why the earlier tests in this file passed: their setUp had just
    written the tokens.

    The fix is structural: one inverse per field, each writing only the column
    it stands for. Not a cache-warming precondition, which would leave the
    safety of the credentials depending on what happened to be read first.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.company.write({"mercadolibre_seller_id": _SELLER})
        row = self.company._meli_auth_row(create=True)
        row.sudo().write({
            "access_token": _ACCESS,
            "refresh_token": _REFRESH,
            "code": _SEED_CODE,
            "token_expires_in": _SEED_EXPIRES_IN,
            "token_refreshed_at": _SEED_REFRESHED_AT,
            "token_expires_at": _SEED_EXPIRES_AT,
        })
        self.env.flush_all()

    def _auth_snapshot(self):
        """The six columns straight from SQL, past every ORM cache."""
        self.env.cr.execute(
            "SELECT %s FROM mercadolibre_auth WHERE company_id = %%s"
            % ", ".join(_AUTH_COLUMNS), (self.company.id,))
        return dict(zip(_AUTH_COLUMNS, self.env.cr.fetchone()))

    def _res_company_xmin(self):
        self.env.cr.execute(
            "SELECT xmin::text FROM res_company WHERE id = %s", (self.company.id,))
        return self.env.cr.fetchone()[0]

    def _write_one_field_cold(self, facade_field, new_value):
        """Write a single credential with nothing warm in the cache."""
        target = _FACADE_TO_AUTH[facade_field]

        self.env.invalidate_all()
        company = self.env["res.company"].browse(self.company.id)

        before = self._auth_snapshot()
        xmin_before = self._res_company_xmin()

        company.write({facade_field: new_value})
        self.env.flush_all()

        after = self._auth_snapshot()

        # Positive guard first. Without proving the targeted column actually
        # moved, "the other five are unchanged" is satisfied by a write that
        # did nothing at all.
        self.assertNotEqual(
            after[target], before[target],
            "writing %s did not change mercadolibre_auth.%s, so the collateral "
            "assertion below would be vacuous" % (facade_field, target))

        collateral = {
            column: (before[column], after[column])
            for column in _AUTH_COLUMNS
            if column != target and before[column] != after[column]
        }
        self.assertEqual(
            collateral, {},
            "writing only %s also changed %s. A shared inverse reads every "
            "field it covers, and the ones that were not in cache read as "
            "False, so credentials nobody touched get wiped. Losing the "
            "refresh token this way is unrecoverable: it is single-use and the "
            "previous one is already spent."
            % (facade_field, ", ".join(sorted(collateral))))

        self.assertEqual(
            self._res_company_xmin(), xmin_before,
            "writing %s issued an UPDATE on res_company" % facade_field)

    def test_each_credential_field_has_its_own_inverse(self):
        """The structural guard behind the behavioural ones below.

        The cold-cache tests catch a shared inverse today. This states the rule
        directly, so re-introducing one is a failure about the rule rather than
        a puzzle about caching.
        """
        inverses = {}
        for name in _CREDENTIAL_FIELDS:
            field = self.env["res.company"]._fields[name]
            inverses[name] = field.inverse

        shared = sorted(
            name for name, inv in inverses.items()
            if list(inverses.values()).count(inv) > 1)
        self.assertEqual(
            shared, [],
            "these credential fields share an inverse: %s. Odoo protects every "
            "field declaring the running inverse, and a protected computed "
            "field that is not cached reads as False, so a shared inverse "
            "wipes the credentials it was not asked to touch."
            % ", ".join(shared))

    def test_writing_only_the_access_token_leaves_the_rest_alone(self):
        self._write_one_field_cold(
            "mercadolibre_access_token", _NEW_ACCESS)

    def test_writing_only_the_refresh_token_leaves_the_rest_alone(self):
        self._write_one_field_cold(
            "mercadolibre_refresh_token", "NEW-REFRESH-value-666666666666")

    def test_writing_only_the_code_leaves_the_rest_alone(self):
        self._write_one_field_cold(
            "mercadolibre_code", "NEW-CODE-value-777777777777")

    def test_writing_only_the_expires_in_leaves_the_rest_alone(self):
        self._write_one_field_cold("mercadolibre_token_expires_in", 10800)

    def test_writing_only_the_refreshed_at_leaves_the_rest_alone(self):
        self._write_one_field_cold(
            "mercadolibre_token_refreshed_at", "2026-07-02 08:30:00")

    def test_writing_only_the_expires_at_leaves_the_rest_alone(self):
        self._write_one_field_cold(
            "mercadolibre_token_expires_at", "2026-07-02 14:30:00")
