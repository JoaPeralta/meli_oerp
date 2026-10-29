# Shared historical order economics

`models/meli_order_economics.py` is the only economic interpreter. It is pure:
HTTP acquisition, seller/account admission, ORM projection, VAT, stock and fiscal
execution remain outside it. A `validated` result is evidence, not permission.

## Supported evidence

| Input | Treatment |
| --- | --- |
| Historical `order_items[].unit_price` | Effective unit price; never subtract promotional funding again. |
| `gross_price` and the documented array `discounts[].amounts.full` | Optional historical reference; full is per unit, gross is for all units. Contradictions refuse. |
| `/orders/{id}/discounts` `discount`, `coupon`, `cashback` | Preserve identified item allocation, quantities, total, seller, derived marketplace contribution and supplier metadata. Missing seller is unknown, not zero. |
| Price/quantity promotions | Documented `discount` allocations are already reflected in the effective price; quantities are not guessed. |
| Coupon | Supported when its identified total explains the buyer-payment difference; never reduce the product invoice basis solely because a coupon exists. Other effects remain pending. |
| Cashback | Audit separately; do not change product price or invoice basis. |
| Multiple items/benefits | Preserve explicit attribution; an ambiguous item/variant allocation stays pending. Identical identified observations deduplicate; conflicting observations refuse. |
| Payments | Corroborate order transaction/paid amounts; optional MP observations must match identity, status, currency and amounts. Retain selected amounts only, not payer/card/token payloads. |
| Shipping | Keep its explicit amount separate. Test whether the reported buyer payment includes it; do not assume that it always does. |

The supported arithmetic subset requires the product effective totals to match
`total_amount`, the identified payment transactions to match that commercial
total, and the identified paid amounts to match `paid_amount`. A buyer-payment
difference must be explained by identified coupons and, when applicable, an
explicit shipping amount. This is a fail-closed implementation subset, **not a
claim that these equalities define every Mercado Libre payment modality**.
Financing allocation, refunds, ambiguous variants and unexplained differences
remain pending/unsupported; no balancing estimates or artificial tolerances run.
Input amounts whose exact decimal product differs from the reported rounded
total also remain pending; universal external rounding semantics are not claimed.
The existing native Odoo tax/currency projection still owns fiscal rounding.

`full_unit_price` is retained as an unverified optional reference, never used to
reconstruct a price. Charges and commission bonuses are not additional product
discounts. MP coupon metadata is corroboration, not the funding authority.
MP payment IDs must come from the seller-admitted order. If MP additionally
reports `collector_id`, it must match that seller; absent collector metadata is
not invented or treated as a second ownership certificate.

## Transport and limitations

An observed HTTP 200 with explicit `details: []` still needs consistent order
and payment evidence. It is not generalized into a universal no-discount API
guarantee. Every other transport error or partial response remains explicit.
Exact `404 discount_not_found` does not establish promotional completeness.
For an owned paid single-item individual without an assigned shipment, complete
independent MP observations can verify its commercial price and produce
`conditioned_draft`, `commercial_status=verified`, `promotional_status=unknown`.
The versioned result retains the HTTP404/error and observed amounts, with
`benefits`, `seller_funded` and `marketplace_funded` null. This is a local guarded
quotation policy, not a claim that the provider contract defines404 as zero.
Every payment ID must come from the admitted seller-owned order and match MP;
any reported collector must corroborate that seller. Explicit MP coupon and
shipping amounts are mandatory for this exception, and order coupons and buyer
differences must reconcile. Unknown fields never become zero. Arbitrary404,
other transport failures and malformed200 do not enter this exception.

All new test fixtures are synthetic arithmetic examples. Unit/source tests do
not establish native ORM atomicity, deployed effectiveness or HOMO readiness.

## Sources and compatibility

- [Mercado Libre orders](https://developers.mercadolibre.com.ar/es_ar/gestiona-ventas),
  inspected 2026-10-27 (page updated 2026-09-21): effective unit price, gross
  formula, discount types, item quantities and `amounts.total/seller` as item
  totals. Supplier metadata is retained without inventing funding-mode effects.
- [Mercado Pago payment reference](https://www.mercadopago.com.ar/developers/es/reference/online-payments/checkout-pro-preferences/get-payment/get):
  transaction amount, coupon and `transaction_details.total_paid_amount` / net
  received remain separate observations, not a universal fiscal basis.
- [Mercado Pago payment fields](https://www.mercadopago.com.ar/developers/es/reference/online-payments/subscriptions/create-payment/post):
  shipping/coupon/payment fields are distinct; no completeness guarantee is inferred.
- [Upstream comparison](https://github.com/ctmil/meli_oerp/blob/19.0/models/orders.py):
  selectively inspected discount fetch, estimator and unit-price projection.
  Those helpers still swallow/default funding evidence and subtract a seller
  aggregate. They are not copied: the pinned fork already has the safer
  effective-price projection. No upstream branch/module was merged.

Rollback is confined to this feature's interpreter, tests and integration seams.
There are no external dependencies and no operational activation in this work.

## Import and upgrade boundary

Traditional individual imports acquire discounts and MP observations before any
commercial model mutation. The existing savepoint contains audit and commercial
projection. The same MP observations are reused, not fetched again after writes.
Accepted evidence merges into `mercadolibre.orders.economic_snapshot` idempotently.
Coupon/charge metadata no longer drives additional product discounts or fee
estimates, and coupon size no longer widens confirmation tolerances.

**A module upgrade of `meli_oerp` is REQUIRED** to create the new JSON column.
No upgrade, deployment, cron activation, confirmation or real import was run.
The manifest version is unchanged: the project policy does not require a bump,
and a version bump cannot replace `-u meli_oerp`. Existing orders without a
validated snapshot require explicit revalidation before automatic amount
acceptance; no historical values are backfilled or silently trusted.
The existing explicit coupon invoice modes `product_discount`/`separate_line`
are not silently changed into `full`: coupon imports under those policies stay
pending as `coupon_invoice_policy_unsupported`. Their old percentage/proration
fallback is not run; supporting a different fiscal transformation requires its
own verified policy, outside this commercial interpreter.

## Bounded evidence correction

Production preflight requires a complete, explicitly identified MP population;
invalid/missing/malformed IDs never fall back to order-only validation and never
fetch a valid-looking prefix. Failed/partial payment observations remain pending.
Audited identifiers accept only bounded printable strings or nonnegative integer
IDs (normalized to strings); currency and funding modes accept bounded strings.
Nested/list/bool/non-finite/overlong metadata is rejected, not copied or silently
sanitized. Optional null remains unknown. No funding-mode enum is invented.

Explicit order coupon identity must correspond to the identified coupon benefit.
Numeric/string representations normalize consistently; contradictory IDs refuse.
Multiple endpoint coupons are retained individually; a singular order identity
cannot select one from that population and stays pending. Absent order identity
is explicitly absent, not an invented match or a zero identifier.

The focused three review reproductions failed before this correction. The final
source matrix passes 282 methods across 14 suites (connector154, Odoo128).
The new native tag `meli_economic_evidence_negative` prepares three real-ORM audit
preservation negatives, but is **NOT RUN**: local Odoo/PostgreSQL is unavailable.
No native atomicity or operational readiness is inferred from source doubles.

## Conditioned draft boundary (evidence version2)

An admitted exact404 with complete independent commercial corroboration projects
only a quotation. The same importer and interpreter serve manual and automatic
routes. The quotation stays draft, even when automatic confirmation is configured;
no invoice acquisition, confirmation, invoice posting or stock fulfillment runs.
The trusted JSON records verified commercial amounts and unknown promotions.
Null benefits/funding are authoritative; legacy Float defaults are not evidence
that the seller or marketplace funded zero. Observed MP coupons remain separate
from effective merchandise prices and never become a second discount.

Only a private lexical importer capability can update an audited draft's proof,
amounts and source bindings. It is bound to cursor, actor, company, remote identity
and elevation state; object identity cannot be supplied by RPC JSON/context flags.
Removing/clearing/rebinding/copying proof or source traceability is refused. Draft
projection authority never permits irreversible effects: direct ORM state writes,
sale confirmation, invoicing/posting, procurement and stock effect entrypoints
check the actual source evidence before calling inherited native operations.
Unaudited unrelated native orders retain their existing behavior and ACL path.

Distinct sanitized observations are retained in a flat, deduplicated `history`.
Exact retries are no-ops. A later complete observation reuses the same graph.
A later incomplete observation cannot erase previously complete knowledge:
`latest_observation` explicitly records current unknown evidence. Changed amounts
never inherit historical promotional completeness, and current unknown evidence
cannot authorize effects from old proof. Confirmed/invoiced history is inspected
before acquisition and is never reprojected by this flow.

Automatic conditioned processing completes its one receipt as `SUCCESS` only with
durable conditioned JSON and no retry time. That state means processing ended,
not permission to sell/invoice/ship. A later independent receipt can revalidate
the same quotation. The existing consumer savepoint contains graph, source audit
and terminal evidence; a fault rolls them back together. Scheduler retry metadata
is recorded only after that rollback; PostgreSQL failures escape to the owner.

### Current deployment classification

This extension adds no field, table, ACL, view, data record or destructive backfill.
The JSON column was already present in the selected current baseline. New Python
guard/model extensions require an exact connector rebuild and process restart;
this change does not itself introduce another schema migration. The earlier
column-creation upgrade requirement above still applies to older installations.
Disposable CI must prove fresh install and `-u meli_oerp,viarengo_circuit_meli`
against the selected baseline, preserved prior history, native regressions and
committed cross-process revalidation. Local source tests do not prove any of
those outcomes. No HOMO/production upgrade, deploy or cron activation is authorized
or claimed here. Rollback requires reverting the coordinated pair/pin, retaining
the existing JSON column and its evidence; never erase audit data on downgrade.
