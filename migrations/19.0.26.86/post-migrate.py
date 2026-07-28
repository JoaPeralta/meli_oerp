# -*- coding: utf-8 -*-
"""Move the MercadoLibre credentials from res_company into mercadolibre_auth.

Deliberately SQL and not ORM. By the time this runs the res.company credential
fields have already been redefined as non-stored proxies onto mercadolibre.auth,
so reading them through the ORM would read the destination — an empty row —
and copy nothing while looking like it worked. The legacy values only exist in
the physical res_company columns, which Odoo leaves in place when a stored
field becomes non-stored, so the copy has to go straight to those columns.

    legacy res_company columns  ->  SQL  ->  mercadolibre_auth

The legacy columns are NOT dropped here. Keeping them one release allows the
migration to be inspected in production. Note carefully what they are and are
not: after the first refresh the auth row holds A2/R2 while the legacy columns
still hold A1/R1, so they are a historical record, NOT a rollback switch.
Rolling back the code requires copying the CURRENT auth row back into the
legacy columns and committing that first. They are never dual-written in
normal operation.

No credential value is logged. Only row counts.
"""

import logging

_logger = logging.getLogger(__name__)

_LEGACY_COLUMNS = (
    'mercadolibre_access_token',
    'mercadolibre_refresh_token',
    'mercadolibre_code',
    'mercadolibre_token_expires_in',
    'mercadolibre_token_refreshed_at',
    'mercadolibre_token_expires_at',
)


def _existing_columns(cr, table, columns):
    cr.execute("""
        SELECT column_name
          FROM information_schema.columns
         WHERE table_name = %s
           AND column_name IN %s
    """, (table, tuple(columns)))
    return {row[0] for row in cr.fetchall()}


def migrate(cr, version):
    if not version:
        return

    present = _existing_columns(cr, 'res_company', _LEGACY_COLUMNS)
    missing = set(_LEGACY_COLUMNS) - present
    if missing:
        # Nothing usable to copy. Reported rather than guessed at.
        _logger.warning(
            "meli_oerp: skipping credential migration, legacy res_company "
            "columns absent: %s", ", ".join(sorted(missing)))
        return

    cr.execute("""
        INSERT INTO mercadolibre_auth (
            company_id, access_token, refresh_token, code,
            token_expires_in, token_refreshed_at, token_expires_at,
            create_uid, create_date, write_uid, write_date)
        SELECT c.id,
               c.mercadolibre_access_token,
               c.mercadolibre_refresh_token,
               c.mercadolibre_code,
               COALESCE(c.mercadolibre_token_expires_in, 0),
               c.mercadolibre_token_refreshed_at,
               c.mercadolibre_token_expires_at,
               1, now() AT TIME ZONE 'UTC', 1, now() AT TIME ZONE 'UTC'
          FROM res_company c
         WHERE (
                   COALESCE(c.mercadolibre_access_token, '')  <> ''
                OR COALESCE(c.mercadolibre_refresh_token, '') <> ''
                OR COALESCE(c.mercadolibre_code, '')          <> ''
               )
           AND NOT EXISTS (
                   SELECT 1 FROM mercadolibre_auth a
                    WHERE a.company_id = c.id
               )
    """)
    created = cr.rowcount

    # A company that already had a row keeps it: this migration never
    # overwrites an auth row, so re-running it cannot clobber a credential
    # that was rotated after the first pass.
    cr.execute("SELECT count(*) FROM mercadolibre_auth")
    total = cr.fetchone()[0]

    _logger.info(
        "meli_oerp: MercadoLibre credentials migrated to mercadolibre.auth "
        "(%s row(s) created, %s row(s) total). Legacy res_company columns "
        "kept for inspection and are NOT a rollback switch.", created, total)
