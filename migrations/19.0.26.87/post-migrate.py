# -*- coding: utf-8 -*-
"""Copy the MercadoLibre credentials into mercadolibre_auth, whatever the
legacy schema happens to look like.

WHY THIS EXISTS ON TOP OF 19.0.26.86
------------------------------------
That migration requires all six legacy columns and returns without copying
anything if any one is missing. The deployment it was written for does not have
three of them:

    installed_version        19.0.26.85
    present   mercadolibre_access_token, mercadolibre_code,
              mercadolibre_refresh_token
    absent    mercadolibre_token_expires_at, mercadolibre_token_expires_in,
              mercadolibre_token_refreshed_at

The expiry columns arrived with the token-metadata change, which that
deployment predates. So the real upgrade path skipped the copy entirely and
created zero rows, leaving the working credentials stranded in columns nothing
reads any more while the connector looked for an auth row that did not exist.

WHAT IT DOES
------------
Copies whichever credential columns are physically present, and defaults the
rest:

    token_expires_in      absent -> 0
    token_refreshed_at    absent -> NULL
    token_expires_at      absent -> NULL

Those are not filler values. They already mean "MercadoLibre never reported a
lifetime", which is exactly the truth when the column never existed, and the
connector treats that as unknown rather than inventing a TTL from it.

Idempotent, and it never overwrites an existing auth row: doing so would replace
live credentials with the stale ones the legacy columns still hold, and
MercadoLibre's refresh tokens are single-use, so those are already spent.

19.0.26.86 is left as it is. It may already have run somewhere, and running both
in sequence is safe.

No credential value is logged. Only row counts.
"""

import logging

_logger = logging.getLogger(__name__)

# Legacy column -> (auth column, SQL literal to use when the column is absent).
# Fixed whitelist: these names are interpolated into SQL, and nothing outside
# this mapping ever is.
_COLUMN_MAP = (
    ('mercadolibre_access_token', 'access_token', 'NULL'),
    ('mercadolibre_refresh_token', 'refresh_token', 'NULL'),
    ('mercadolibre_code', 'code', 'NULL'),
    ('mercadolibre_token_expires_in', 'token_expires_in', '0'),
    ('mercadolibre_token_refreshed_at', 'token_refreshed_at', 'NULL'),
    ('mercadolibre_token_expires_at', 'token_expires_at', 'NULL'),
)

# Without at least one of these there is nothing worth migrating.
_REQUIRED_ANY = ('mercadolibre_access_token', 'mercadolibre_refresh_token')


def _present_columns(cr, table, columns):
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

    legacy_names = [legacy for legacy, _auth, _default in _COLUMN_MAP]
    present = _present_columns(cr, 'res_company', legacy_names)

    if not present & set(_REQUIRED_ANY):
        # A fresh install, or an upgrade where the columns are already gone.
        # Nothing to copy, and nothing wrong with that.
        _logger.info("meli_oerp: no legacy MercadoLibre credential columns on "
                     "res_company; nothing to migrate")
        return

    selected = []
    for legacy, _auth, default in _COLUMN_MAP:
        selected.append('c.%s' % legacy if legacy in present else default)

    # Only copy for companies that actually carry credentials, and never over an
    # auth row that already exists.
    non_empty = ' OR '.join(
        "COALESCE(c.%s, '') <> ''" % name
        for name in _REQUIRED_ANY if name in present)

    cr.execute("""
        INSERT INTO mercadolibre_auth (
            company_id, access_token, refresh_token, code,
            token_expires_in, token_refreshed_at, token_expires_at,
            create_uid, create_date, write_uid, write_date)
        SELECT c.id, %s,
               1, now() AT TIME ZONE 'UTC', 1, now() AT TIME ZONE 'UTC'
          FROM res_company c
         WHERE (%s)
           AND NOT EXISTS (
                   SELECT 1 FROM mercadolibre_auth a WHERE a.company_id = c.id
               )
    """ % (', '.join(selected), non_empty))
    created = cr.rowcount

    cr.execute("SELECT count(*) FROM mercadolibre_auth")
    total = cr.fetchone()[0]

    missing = [name for name in legacy_names if name not in present]
    _logger.info(
        "meli_oerp: MercadoLibre credentials migrated to mercadolibre.auth "
        "(%s row(s) created, %s row(s) total). Legacy columns absent and "
        "defaulted: %s. Existing auth rows were left untouched.",
        created, total, ", ".join(missing) or "none")
