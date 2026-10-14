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
