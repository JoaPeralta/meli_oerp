"""Targeted token-URL cleanup on an explicitly approved module upgrade.

No external API, token refresh, mail history, attachments or filestore changes.
The upgrade must only run after a consistent PostgreSQL + filestore backup.
"""


def migrate(cr, version):
    # The old label implementation persisted MercadoLibre URLs containing
    # bearer tokens. Clear only those two owned fields and no other data.
    cr.execute("""
        UPDATE mercadolibre_shipment
           SET pdf_link = NULL
         WHERE pdf_link IS NOT NULL
           AND pdf_link ILIKE '%%access_token=%%'
    """)
    # This is a TransientModel table, but old rows may survive until vacuum.
    cr.execute("""
        UPDATE mercadolibre_shipment_print
           SET full_links = '{}'
         WHERE full_links IS NOT NULL
           AND full_links ILIKE '%%access_token%%'
    """)
