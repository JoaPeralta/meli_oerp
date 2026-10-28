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

`full_unit_price` is retained as an unverified optional reference, never used to
reconstruct a price. Charges and commission bonuses are not additional product
discounts. MP coupon metadata is corroboration, not the funding authority.

## Transport and limitations

An observed HTTP 200 with explicit `details: []` still needs consistent order
and payment evidence. It is not generalized into a universal no-discount API
guarantee. Every other transport error or partial response remains explicit.
Exact `404 discount_not_found` yields
`discount_not_found_evidence_incomplete`: neither scalar gross equality nor
missing/zero coupon or charge fields proves exhaustive absence of benefits.
No successful 404 provider-contract fixture is invented. Independent sufficient
official evidence is still required to implement that acceptance scenario.

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
