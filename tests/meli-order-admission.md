# Individual order snapshot contract

The pure admission API validates one own order and returns an independent copy.
It does not create an SDK client, access Odoo records or import a commercial sale.

## API

- `remote_id(value)` accepts positive ASCII decimal string/integer identities.
- `item_key(item_id, variation_id)` retains publication and nullable variation.
- `verify_order(payload, seller_id, expected_id=None, expected_pack=None)` checks
  individual order/seller/pack identity and returns a deep-copied source body.
- `enrich_buyer(current, refreshed, seller_id)` replaces only verified buyer data.
- `AdmissionError` rejects missing, contradictory or unsupported source data.

## Preserved boundaries

Commercial quantities must be positive integers; unit prices finite and nonnegative.
Order and line currencies must match. Duplicate source-item identities are rejected.
A nonempty status is required without inventing a provider status enumeration.
Cancelled, invalid, delivered, detected split and bundle shapes remain unsupported.
Buyer refresh cannot replace captured economics or change source membership.

## Activation and verification

This contract is executable independently; importer activation is a later unit.
Run the standalone `test_meli_t4_pack_admission.py` suite from the repository root.
No ORM field, data/security record, migration or commercial pack support is added.

## Shipment membership snapshots

`snapshot_response(response)` copies body, status and available header metadata.
Capture it before another GET: the transport may reuse one mutable response object.
`collect_membership` handles only the flat shipment/order association subset.
Every accessible own sibling is individually verified with the M1 order contract.
Known foreign members are never requested; unknown ownership never permits probing.
HTTP403/404 preserves the verified own subset with membership explicitly incomplete.
HTTP204 remains pending, not an empty successful pack or permission to discard it.
Malformed/unavailable essential responses raise; transport errors propagate unchanged.
Logistic requested quantities never replace individual commercial quantities.
This API depends on M1 and accepts an injected client; it constructs no SDK client.
Production importer activation, durable line binding and pack commerce remain later units.

## Private actor and existing-history contract

`_meli_assert_order_admission_actor(config)` requires the configured active internal actor.
The actor must be non-superuser and restricted to the configured single company.
The method returns a validated remote seller identity, not the Odoo actor identifier.
`_meli_preflight_order_history(payload, config, local_id=None)` inspects visible roots.
It does not elevate access, write records or guess missing stored seller ownership.
Existing order/shipment/company/seller bindings and an optional local ID must agree.
Existing sale roots must be draft/sent, without pickings, invoices or an update prohibition.
Both private methods are complete callable contracts; production callers are unchanged.
These generic checks alone do NOT enforce Circuit A/B or replace installed record rules.
Circuit policy requires the later bridge override and guarded importer activation units.
Extracted-body source tests are not native ORM/ACL or commercial-acceptance proof.

Same-pack shipment parents must pass the full stored ownership/history checks.
Their company, configured Odoo actor and serialized remote seller are checked together.
