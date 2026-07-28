# -*- coding: utf-8 -*-
"""The credential migration has to survive a partial legacy schema.

``19.0.26.86/post-migrate.py`` requires all six legacy columns and returns
without copying anything if any one of them is missing. That looked defensive
and is in fact the failure mode, because the deployment this change is aimed at
does not have three of them.

Observed on the live database, read-only:

    installed_version        19.0.26.85
    mercadolibre_auth        does not exist
    present   mercadolibre_access_token, mercadolibre_code,
              mercadolibre_refresh_token
    absent    mercadolibre_token_expires_at, mercadolibre_token_expires_in,
              mercadolibre_token_refreshed_at

The three expiry columns were introduced by the token-metadata change, which
that deployment predates. So on the real upgrade path the migration finds them
missing, skips entirely, and creates **zero** rows. The credentials stay
stranded in the legacy columns while the connector starts reading an auth row
that does not exist — and a refresh cannot even be attempted, because
``SELECT ... FOR UPDATE`` on a missing row locks nothing and is treated as
``AUTH_CRITICAL`` by design.

The fix is a `19.0.26.87` migration that copies what is there and defaults what
is not. The absence of an expiry column means the lifetime is unknown, which is
already a state the connector handles: `0` and `NULL` mean "not reported", and
nothing is invented from them.

`19.0.26.86` is left exactly as it is. It may already have run somewhere, and
`.87` is idempotent, so running both in sequence is safe.
"""

import importlib.util
import os

from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

_SELLER = "2288636236"
_ACCESS = "LEGACY-ACCESS-000000000000-%s" % _SELLER
_REFRESH = "LEGACY-REFRESH-111111111111"
_CODE = "LEGACY-CODE-222222222222"

_LEGACY_CREDENTIAL_COLUMNS = (
    "mercadolibre_access_token",
    "mercadolibre_refresh_token",
    "mercadolibre_code",
)
_LEGACY_EXPIRY_COLUMNS = (
    ("mercadolibre_token_expires_in", "integer"),
    ("mercadolibre_token_refreshed_at", "timestamp"),
    ("mercadolibre_token_expires_at", "timestamp"),
)


def _load_migration(version):
    """Load a migration script by path. They are not importable as modules."""
    module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(module_root, "migrations", version, "post-migrate.py")
    spec = importlib.util.spec_from_file_location(
        "meli_oerp_migration_%s" % version.replace(".", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@tagged("post_install", "-at_install")
class TestAuthRowMigration(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.user.company_id
        self.env.flush_all()
        # Start from no auth row: this is the state an upgrade begins in.
        self.env.cr.execute("DELETE FROM mercadolibre_auth WHERE company_id = %s",
                            (self.company.id,))

    # ------------------------------------------------------------------
    # schema simulation
    # ------------------------------------------------------------------
    def _add_legacy_columns(self, with_expiry):
        """Recreate the physical columns an older deployment still carries.

        After the dedicated-row change these fields are non-stored, so a fresh
        install has no such columns at all. The upgrade path does, because Odoo
        leaves a column in place when its field stops being stored.
        """
        cr = self.env.cr
        for name in _LEGACY_CREDENTIAL_COLUMNS:
            cr.execute("ALTER TABLE res_company ADD COLUMN IF NOT EXISTS %s varchar"
                       % name)
        if with_expiry:
            for name, sql_type in _LEGACY_EXPIRY_COLUMNS:
                cr.execute("ALTER TABLE res_company ADD COLUMN IF NOT EXISTS %s %s"
                           % (name, sql_type))
        cr.execute(
            "UPDATE res_company SET mercadolibre_access_token = %s, "
            "mercadolibre_refresh_token = %s, mercadolibre_code = %s WHERE id = %s",
            (_ACCESS, _REFRESH, _CODE, self.company.id))

    def _auth_rows(self):
        self.env.cr.execute(
            "SELECT access_token, refresh_token, code, token_expires_in, "
            "token_refreshed_at, token_expires_at "
            "FROM mercadolibre_auth WHERE company_id = %s", (self.company.id,))
        return self.env.cr.fetchall()

    def _assert_legacy_credentials_are_there(self):
        """Positive guard.

        "The migration created no rows" means nothing unless there was
        something to copy in the first place.
        """
        self.env.cr.execute(
            "SELECT mercadolibre_access_token, mercadolibre_refresh_token "
            "FROM res_company WHERE id = %s", (self.company.id,))
        access, refresh = self.env.cr.fetchone()
        self.assertEqual(access, _ACCESS, "the legacy fixture was not written")
        self.assertEqual(refresh, _REFRESH, "the legacy fixture was not written")

    # ------------------------------------------------------------------
    # the defect, characterised
    # ------------------------------------------------------------------
    @mute_logger("odoo.addons.meli_oerp.migrations.19.0.26.86.post-migrate")
    def test_2686_copies_nothing_when_the_expiry_columns_are_absent(self):
        """Reproduces the live upgrade path exactly.

        Not a failing test: it pins the behaviour that blocks the deployment, so
        that the reason `.87` exists stays visible.
        """
        self._add_legacy_columns(with_expiry=False)
        self._assert_legacy_credentials_are_there()

        _load_migration("19.0.26.86").migrate(self.env.cr, "19.0.26.85")

        self.assertEqual(
            self._auth_rows(), [],
            "if this ever starts copying, the reason for 19.0.26.87 is gone "
            "and it should be revisited")

    # ------------------------------------------------------------------
    # the fix
    # ------------------------------------------------------------------
    def test_2687_copies_the_credentials_when_the_expiry_columns_are_absent(self):
        self._add_legacy_columns(with_expiry=False)
        self._assert_legacy_credentials_are_there()

        _load_migration("19.0.26.87").migrate(self.env.cr, "19.0.26.85")

        rows = self._auth_rows()
        self.assertEqual(len(rows), 1, "the credentials were not migrated")
        access, refresh, code, expires_in, refreshed_at, expires_at = rows[0]
        self.assertEqual(access, _ACCESS)
        self.assertEqual(refresh, _REFRESH)
        self.assertEqual(code, _CODE)
        # A missing column means the lifetime was never reported. 0 and NULL say
        # exactly that; nothing is invented from them.
        self.assertEqual(expires_in, 0)
        self.assertIsNone(refreshed_at)
        self.assertIsNone(expires_at)

    def test_2687_copies_the_expiry_metadata_when_the_columns_do_exist(self):
        """The other upgrade path: a deployment that did carry them."""
        self._add_legacy_columns(with_expiry=True)
        self.env.cr.execute(
            "UPDATE res_company SET mercadolibre_token_expires_in = 21600, "
            "mercadolibre_token_refreshed_at = '2026-07-01 10:00:00', "
            "mercadolibre_token_expires_at = '2026-07-01 16:00:00' WHERE id = %s",
            (self.company.id,))

        _load_migration("19.0.26.87").migrate(self.env.cr, "19.0.26.85")

        rows = self._auth_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 21600)
        self.assertIsNotNone(rows[0][4])
        self.assertIsNotNone(rows[0][5])

    def test_2687_does_not_clobber_an_existing_auth_row(self):
        """A `.86` that already ran, or a token rotated since, must survive.

        Overwriting here would replace live credentials with the stale ones the
        legacy columns still hold — and since MercadoLibre's refresh tokens are
        single-use, those are already spent.
        """
        self._add_legacy_columns(with_expiry=False)
        self.env.cr.execute(
            "INSERT INTO mercadolibre_auth (company_id, access_token, "
            "refresh_token, code, token_expires_in, create_uid, create_date, "
            "write_uid, write_date) VALUES (%s, %s, %s, %s, %s, 1, now(), 1, now())",
            (self.company.id, "CURRENT-ACCESS-999", "CURRENT-REFRESH-999", "", 21600))

        _load_migration("19.0.26.87").migrate(self.env.cr, "19.0.26.85")

        rows = self._auth_rows()
        self.assertEqual(len(rows), 1, "the migration created a second row")
        self.assertEqual(rows[0][0], "CURRENT-ACCESS-999",
                         "the migration overwrote credentials already in use")
        self.assertEqual(rows[0][1], "CURRENT-REFRESH-999")

    def test_2687_is_idempotent(self):
        self._add_legacy_columns(with_expiry=False)
        migration = _load_migration("19.0.26.87")

        migration.migrate(self.env.cr, "19.0.26.85")
        first = self._auth_rows()
        migration.migrate(self.env.cr, "19.0.26.86")
        second = self._auth_rows()

        self.assertEqual(len(first), 1, "the first run did not migrate")
        self.assertEqual(first, second,
                         "a second run changed the stored credentials")

    def test_2687_creates_nothing_when_there_is_nothing_to_copy(self):
        """No legacy columns at all: a fresh install, not an upgrade."""
        _load_migration("19.0.26.87").migrate(self.env.cr, "19.0.26.85")

        self.assertEqual(self._auth_rows(), [])

    def test_2687_never_logs_a_credential(self):
        self._add_legacy_columns(with_expiry=False)

        with self.assertLogs(
                "odoo.addons.meli_oerp.migrations.19.0.26.87.post-migrate",
                level="INFO") as captured:
            _load_migration("19.0.26.87").migrate(self.env.cr, "19.0.26.85")

        text = "\n".join(r.getMessage() for r in captured.records)
        for secret in (_ACCESS, _REFRESH, _CODE):
            self.assertNotIn(secret, text,
                             "the migration logged a credential")
