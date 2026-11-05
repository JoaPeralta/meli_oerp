# P0: tokenless MercadoLibre labels, product links and image access

## Scope / status

This is a **source-level fix** awaiting CI, code review, an explicitly approved
Odoo module upgrade and *real* normal-user HOMO acceptance. No production,
HOMO, OAuth token or database was modified while preparing the PR.

Upstream references:
- https://developers.mercadolibre.com.ar/es_ar/autenticacion-y-autorizacion
- https://developers.mercadolibre.com.ar/es_ar/mercadoenvios-modo-2
- https://developers.mercadolibre.com.ar/es_ar/trabajar-con-imagenes

### Data flow

- Item API links no longer contain tokens; authenticated item GETs remain
  server-side through the already existing MeLi client.
- Shipping label bytes come from the fixed MercadoLibre endpoint through a
  bounded Bearer request; seller, company, order and document visibility are
  checked first. The individual format remains shipment.pdf_file; grouped
  and automatic exports are stored in **caller-owned** mercadolibre.shipment.print
  transient binaries, served by Odoo's authenticated /web/content handler.
- Requests are limited to 50 label IDs per upstream batch and cannot inject a
  provider hostname, path, arbitrary URL or bearer token into the browser.
- Public product images must be both ORM-readable and website-published; image
  IDs must belong to that product's template or the exact requested variant.

### Module upgrade

The manifest advances from 19.0.26.201 to 19.0.26.401 because the
transient label export gained print_file and print_filename fields, and
existing label views changed. This is a **database upgrade**, not just a Python
restart. Running new Python against an old registry is not accepted.

The 19.0.26.401 post-migrate script clears only:
- mercadolibre_shipment.pdf_link rows containing access_token=.
- mercadolibre_shipment_print.full_links rows containing access_token.

The script leaves pdf_file, pdfimage_file, attachments, mail_message,
filestore, sales, fiscal artifacts and unrelated URLs untouched. It is idempotent.
It runs **only after the separate, explicitly authorized module upgrade**.

### Preflight / dry-run

Before approving an upgrade in HOMO, capture a consistent, independently
restorable PostgreSQL + filestore pair. Inspect affected counts without selecting
or logging the raw values:

~~~sql
SELECT count(*) FROM mercadolibre_shipment
 WHERE pdf_link ILIKE '%access_token=%';
SELECT count(*) FROM mercadolibre_shipment_print
 WHERE full_links ILIKE '%access_token%';
SELECT count(*) FROM mail_message
 WHERE body ILIKE '%access_token=%';
~~~

The third query is **inventory only**: mail history is not rewritten by the
migration. Separately examine application logs and backup retention policies
without displaying tokens, headers, cookies or PII. Do not automatically scrub
immutable audit history or physically alter old backups. Decide with the owner
whether token revocation/rotation is necessary when credible exposure exists.

### Required HOMO acceptance after approved deployment + module upgrade

- Assert current deployed Odoo/connector pins, actual ir_module_module version
  and effective installed fields/views, not just a green build.
- Normal seller operator: authorized item link, private/public images and normal
  product upload. Test cross-product image ID, cross-company and private cases.
- Normal operator: individual PDF, ZPL/ZPL2, grouped batches of 1, 50, 51+,
  automatic printing where the existing workflow uses it. Match exact stored
  artifacts and downloadable binary bytes, including correctly named ZIP/ZPL.
- Negative cases: cross-company, wrong seller, unknown shipping reference,
  denied order, unprintable status, invalid/oversized provider response and
  exhausted authorization; no external request or artifact creation occurs
  before the record checks pass.
- Validate snapshot/attachment and old-field cleanup, no duplicate writes,
  no token in URL, wizard, error, HTML, database or log.
- Keep commercial API, stock synchronization, cron jobs and ARCA emission OFF.

**Important:** Hermetic source tests and disposable Odoo CI cannot prove a
runtime public-website path or actual printer. Keep this PR as draft until
those limits have been addressed and the owner approves promotion.

### Rollback

While source is not deployed: delete only this feature branch if rejected.
After a controlled module upgrade, a code-only rollback does **not** undo schema
and data migrations. Restore the verified consistent PostgreSQL + filestore
pair and the matching image pin, and verify identity before reopening traffic.
