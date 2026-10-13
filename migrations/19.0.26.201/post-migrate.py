"""Initialize absent local counts only; preserve notification history and due times.

Module upgrade creates the fields/index before this post-migration. Fresh
install uses the model default. No notification is requeued or classified here.
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'mercadolibre_notification'
          AND column_name = 'local_attempts'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        UPDATE mercadolibre_notification
        SET local_attempts = 0
        WHERE local_attempts IS NULL
    """)
