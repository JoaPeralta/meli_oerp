# Keep Mercado Libre posting metadata independent of commercial data

Opening a posting now reads stored fields only. Explicit metadata requests fetch a
single Mercado Libre item per distinct item ID, without importing questions or
refreshing OAuth. All writes are to the posting row; prices on products or in
Mercado Libre and stock in either system are outside this workflow.

## Behavior and identity

| Path | Behavior |
| --- | --- |
| Catalogue or product import | Requires an observed HTTP 200, matching item and configured seller before matching or writes. Bulk matching starts from the legacy basic response; before any candidate write it corroborates the same product and variation keys against a separately admitted detailed response. Enrichment can veto but cannot select a different product. Product creation reuses that checked detail. A new posting receives valid fields; an existing unbound posting fills only missing fields when bound. An already bound posting is not backfilled by bulk import. |
| Form read | `Stored Metadata` is a local completeness indicator, not a claim that the item was queried or is current. The optional SKU does not control this indicator. |
| Questions | Opening a question form builds its answer link from local country/site configuration only. **Fetch Mercado Libre Questions** is a separate manager action for selected postings; it does not refresh metadata, reconnect OAuth or switch jobs on. Scheduled/import and legacy notification entry points retain the company option; a disabled, unprocessed, ambiguous or erroneous notification no longer reports success. |
| Refresh Posting Metadata | Existing connector-manager permissions, explicit selection, current company and corroborated binding. Valid current metadata can replace older metadata on an already core-complete posting. The wizard skips historical core gaps and preserves known values when the response has no valid replacement. |
| Historical repair | Private server-side operation, existing administrator and connector-manager roles, native access checks, explicit posting row IDs and active company. Apply only fills missing values and requires the exact digest from the reviewed preview. |

Price and SKU require a proven offer scope: an explicit empty `variations` list,
a valid same-site modern User Product when `variations` is omitted, or an exact
legacy variation. An omitted field by itself does not establish a standalone
listing. Item-level status and the safe public permalink may still be proposed
for an independently proven binding; root offer fields are omitted. Reports
distinguish absent, empty, invalid, not applicable, unproven and valid fields.

The effective SKU is drawn from the exact offer object in priority order:
`SELLER_SKU` attribute, `seller_sku`, then legacy `seller_custom_field`.
Explicit SKU values must agree and must not claim a different visible product.
The legacy custom field is a fallback reference for existing catalogue matching
and a metadata value, not independent proof for manual refresh or repair. If it
actually resolves to a different product, however, it vetoes the operation.
A different visible owner, local User Product mismatch, duplicate candidate or
archived owner prevents acceptance even when another signal matches. A known
legacy variation conflict stops the direct catalogue import before any writes:
the legacy operation can rebuild template values or update stock across siblings.
An independently proven offer can still be handled by the metadata-only actions.

Existing postings have no standalone seller/owner column. Explicit metadata
actions require a product owned by the active company, a unique exact local
item/variation posting, matching remote seller, and independent product
corroboration. No relink is performed. An order-derived posting can still begin
as an intentional placeholder: its paid unit price is not the current offer
price. Conventional catalogue imports still perform their pre-existing
commercial operations for admitted items; the new metadata actions do not
share those operations.

## Historical safety boundary

The default preview returns bounded per-row proposals and reasons plus a digest.
The digest binds the database, actor, company, selection, prior values,
corroborated identity and candidate values. The report includes the validated
price, status and public permalink; SKU is redacted. Apply re-fetches evidence
before locking, compares the digest, then locks and revalidates the selected
posting/product/template/company rows in one savepoint before writing. No
implicit commit. A different or contended plan must be previewed again. Running
a fresh preview after a successful application yields no proposal for fields
already filled.

Apply requires a separately authorized, serialized maintenance window with
catalogue and binding writers excluded. Row locks do not prevent a different
transaction from inserting a new duplicate key. The model does not expose
historical apply via RPC or the wizard. No cron, implicit upgrade repair, or
schedule is added.

The new GET capability rejects neutralized databases by default, uses the active
company's restricted capability without token refresh, sends a bearer header
only to the fixed Mercado Libre HTTPS host, and refuses redirects and ambient
proxies. It does not honor the existing optional HTTP proxy. Redirect and error
bodies are not read. A successful response must use identity encoding; at most
512 KiB plus one overflow-detection byte is read before JSON parsing. This
bounds the body admitted to the parser, not all interpreter allocation, response
headers, or total elapsed request time. Synthetic tests use a mocked adapter
and no live credentials.

## Upgrade and rollback

Both actions and their bound forms in `views/posting_view.xml` are database
records. A later authorized release requires an Odoo module upgrade to make
those changes effective. The core read behavior changes with Python, but this
alone does not prove the effective form/action. Verify exact source version,
module upgrade, effective normal-manager UI/permissions, and read behavior in
an isolated database before deployment. Rollback code and effective view records
together; do not silently reverse successfully audited metadata fills.
