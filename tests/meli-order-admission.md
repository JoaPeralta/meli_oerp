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
