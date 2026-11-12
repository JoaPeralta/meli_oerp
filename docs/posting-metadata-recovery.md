# Recover incomplete Mercado Libre postings without changing offers

## Diagnosis

Production reference supplied with the request: Odoo image source `4a36a82319f91d77db1e8b9e07505265cc0891fd`, pinned connector `67fc3e9ea06a6a8128652bf491f8a8cefbcb532d`. The pin is confirmed in that Odoo source; the 7 November successful build is user-supplied evidence, not a new Railway inspection.

The prior production list showed 124 distinct MLAs, 42 (33.9%) lacking status, price and permalink together, 38 products with multiple MLAs, and empty posting SKU and legacy variation-ID columns. These are observed list values, not a row-by-row execution history. No verified read-only database role or historical server logs were provided. Do not open the old posting form to recover IDs: computing the displayed status can call Mercado Libre and write Odoo. The company connection form can also invoke the refresh path.

| Route in the deployed source | What it explains |
| --- | --- |
| `models/company.py:1085–1117,1235–1301` | The catalogue import already has item data, but its existing-product link creates a posting without status, price, permalink or SKU. Its match tuple's SKU is not guaranteed to represent all item SKUs. |
| `models/product.py:2452–2476,2642–2651` | The product import resolves an exact posting, then historically re-fetched the item through the questions handler. |
| `models/orders.py:4764–4779,4891–4899` | An order may create a placeholder and bind it later. Order unit price is historical sale evidence, not the current posting price. |
| `models/product.py:4694–4702` | Outbound publishing has a legacy MLA-only posting update. It is not an input or a prerequisite for repairing local metadata. |
| `models/posting.py:193–256,291`, `views/posting_view.xml:44` | Reading a computed field triggered an item request and possible write. Its `ok` preceded validation. |
| `models/company.py:358–369,1039–1054`, `models/notification.py:375–395,521–537` | The questions job used to refresh three fields, never SKU, and needs both a scheduled job and the company flag. Question and item events have separate callers; the item fallback is a status-only first-match update. Jobs being off can explain missing retries, not the initial omission. |

There is no proof of which refresh ran or failed for each row. A blank legacy variation is not necessarily wrong in the User Products model. An empty SKU does not prove the provider omitted it. In variant responses, do not infer a SKU or User Product from a sibling variation.

## Multiple offers and deliberately separate risks

- ShineMate ES700 (`SM6LRES700-6/5`): MLA1651548355 (ARS 780,000, six installments) and MLA1651389099 (ARS 944,585, nine installments) share the product and marketplace User Product `MLAU3747606772`. Do not merge the offers or turn their price difference into an Odoo product-price adjustment. Order lines retain the sold MLA and historic price (`models/orders.py:4906–4919,5022–5037`).
- **High priority, separate:** validate stock by marketplace User Product and actual location, never sum sibling offer quantities from the same pool. Local and Full are independent. The outbound stock route rejects modern User Products, multiorigin and Full (`models/product.py:4773–4832`); actual quantities, reservations and per-offer routing were not inspected. The companion Full implementation is not certified by this change.
- **High priority, separate:** price publication uses a scalar product MLA and a product-level value (`models/product.py:4915–5019`), not an independent pricing policy for all offers. Do not enable price publishing until each offer is verified. Cost and valuation belong to the product/movements, not the publication row; net margin per sold MLA also depends on actual charges. No inventory, sale pricing, costing, order or accounting logic should change in this patch.

## Deployment and recovery gates

1. **Keep production unchanged.** Obtain separate authorization for any future rollout and then for any actual repair. An isolated fresh install is not evidence of deployed views, ACLs or record rules.
2. **Prepare evidence.** An authorized operator should take a database/filestore backup and verify restore. Collect the 42-row candidate list using a separately authorized PostgreSQL read-only role and read-only transaction, or an equivalent verified read-only snapshot. Include record IDs, MLA/variation, product/reference, creation and modification timestamps and stored metadata. Correlate sanitized logs by MLA/timestamp only; don't export credential fields.
3. **Validate a restored copy.** No real OAuth, outbound network, automated jobs, orders or invoicing. Check native Odoo permissions with allowed and disallowed users, effective posting view, two independent offer prices, dry-run behavior, duplicate/stale/foreign/ambiguous outcomes, and idle cron states.
4. **Roll out only after approval.** Pin the accepted connector commit and classify changes to fields, views and security data. If view XML changes, run the approved `-u meli_oerp` in the selected database with the documented backup and rollback procedure. A code deploy alone cannot load the view; if applicable report `CODE DEPLOYED — DB UPGRADE PENDING`. Verify module state, XML IDs, effective view, real-user permissions and that opening the view does not request MeLi or write.
5. **Review the repair before execution.** Pilot the specific incomplete ShineMate MLA and a complete sibling, then preview bounded explicit batches from the 42-row list. Check seller, exact item/variation, product proof, field-level diffs and reasons for omissions. Leave ambiguous records untouched. Use a separately authorized operator for apply, with the exact reviewed selection/evidence; re-preview if stale. Validate an idempotent re-run. Never use the generic sync or questions job as repair.
6. **Rollback.** Revert the connector pin and restore/reapply the matching stored view if needed. For an authorized applied repair, retain before/after field evidence separately so only metadata changed by that repair can be restored after checking for newer changes. Do not revert unrelated data or use a wholesale production restore without the normal incident approval.

### Read-only inventory query (future authorized operator)

Use a role with SELECT-only grants and disable query/credential output outside the approved operator context. This query does not call the Odoo ORM. It matches the observed three-empty-field cohort, rather than assuming an empty SKU or variation is an error. Keep the result internal; an exact row selection and seller corroboration are still required.

```sql
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5s';
SELECT mp.id, mp.meli_id, mp.meli_variation_id, mp.product_id,
       pp.default_code, pt.company_id, mp.create_date, mp.write_date,
       mp.meli_status, mp.meli_price, mp.meli_permalink,
       mp.meli_seller_custom_field
FROM mercadolibre_posting AS mp
LEFT JOIN product_product AS pp ON pp.id = mp.product_id
LEFT JOIN product_template AS pt ON pt.id = pp.product_tmpl_id
WHERE NULLIF(btrim(mp.meli_status), '') IS NULL
  AND NULLIF(btrim(mp.meli_price), '') IS NULL
  AND NULLIF(btrim(mp.meli_permalink), '') IS NULL
ORDER BY mp.id;
ROLLBACK;
```

Check local conflicts by exact `(meli_id, coalesce(meli_variation_id, ''))` in the full permitted cohort, not by product or SKU alone. `company_id` above belongs to the product, not a proven owner of the posting. Treat the returned URLs as sensitive until sanitized; do not publish or follow historical links to establish ownership.

### Historical repair interface (do not run on production as part of this change)

The review-only server method is private, not a web action. It requires the system administrator and Mercado Libre manager roles, ordinary record permissions, a currently configured seller and independently proven product identity. Shared-company products or ambiguous variants/SKUs are skipped. Preview and apply use an existing valid access token for a fixed read-only item request, never refresh OAuth, follow redirects, or write to the provider. No token, raw API body, or SKU is printed. An expired token is an omission, not authorization to reconnect.

In an authorized isolated shell, with explicitly reviewed local posting IDs and the correct company/user, preview first:

```python
preview = env['mercadolibre.posting']._meli_repair_metadata(posting_ids)
```

The report gives a plan fingerprint, validated proposed metadata, availability states and skip reasons. A field already present is not overwritten. Obtain separate approval before any real apply. The authorized operator must use the same user, company and row IDs, supplying the exact reviewed plan as `reviewed_plan` with `dry_run=False`. The method rechecks evidence and never commits by itself. If the plan is stale or rows are busy, re-preview; never force it. Actual apply requires a short maintenance window that excludes concurrent catalogue and binding writers. Row locks protect the selected posting, company and linked product/template; they do not make historical exact keys, SKUs or User Products globally unique. Keep before/after evidence and validate a second preview before advancing to the next batch.
