# Keep Mercado Libre posting metadata independent of commercial data

Opening a posting now reads stored fields only. Explicit metadata requests fetch a
single Mercado Libre item per distinct item ID, without importing questions or
refreshing OAuth. All writes are to the posting row; prices on products or in
Mercado Libre and stock in either system are outside this workflow.

## Behavior and identity

| Path | Behavior |
| --- | --- |
| Catalogue or product import | Uses a successful HTTP 200 item's metadata only when the item and configured seller agree. A new posting receives valid fields; an existing unbound posting fills only missing fields when bound. An already bound posting is not backfilled by bulk import. |
| Form read | `Stored Metadata` is a local completeness indicator, not a claim that the item was queried or is current. The optional SKU does not control this indicator. |
| Questions | Explicit questions processing still uses its existing option and authorization path. It no longer updates item metadata. |
| Refresh Posting Metadata | Existing connector-manager permissions, explicit selection, current company and corroborated binding. Valid current metadata can replace older metadata on an already core-complete posting. The wizard skips historical core gaps and preserves known values when the response has no valid replacement. |
| Historical repair | Private server-side operation, existing administrator and connector-manager roles, native access checks, explicit posting row IDs and active company. Apply only fills missing values and requires the exact digest from the reviewed preview. |

Validated price is the item's price for a standalone listing, or the exact
variation price for a legacy variant. SKU is extracted from that same object
in priority order: `SELLER_SKU` attribute, `seller_sku`, then legacy
`seller_custom_field`. Contradictory values within the same source are invalid;
lower-priority legacy values do not override the effective SKU. Legacy custom
values may be shown as metadata but are not product identity evidence. A
listing-level record with legacy variations does not take a variant's SKU or
price. Reports distinguish absent, explicitly empty, invalid, not applicable,
and valid fields; an invalid item response is reported separately.

Existing postings have no standalone seller/owner column. The explicit actions
require a product owned by the active company, an exact and unique local
item/variation posting, matching remote seller, and independently corroborated
product identity (exact product item/variation, unique effective SKU, or unique
matching User Product). Conflicts, shared-company ownership, incomplete identity
and ambiguous rows are skipped. No relink is performed. An order-derived posting
can still begin as an intentional placeholder: its paid unit price is not the
current offer price.

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
proxies. It does not honor the existing optional HTTP proxy. Test this flow with
synthetic mocked transport in an isolated database. Configure no live
credentials for synthetic testing.

## Upgrade and rollback

The action/wizard wording and button in `views/posting_view.xml` are database
records. A later authorized release requires an Odoo module upgrade to make
those changes effective. The core read behavior changes with Python, but this
alone does not prove the effective form/action. Verify exact source version,
module upgrade, effective normal-manager UI/permissions, and read behavior in
an isolated database before deployment. Rollback code and effective view records
together; do not silently reverse successfully audited metadata fills.
