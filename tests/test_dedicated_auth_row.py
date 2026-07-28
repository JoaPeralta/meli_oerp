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
