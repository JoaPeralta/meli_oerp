# P0 security correction: deployment and prepared cleanup

The connector now uses token-free product links and local, authenticated label URLs. Label credentials stay in the existing server-side OAuth capability. Image requests must prove product/variant/template ownership and effective read access; anonymous and portal requests additionally require website publication.

## Review path

1. `models/product.py`: credential-free API permalink and predictor parameters; existing SDK translates the access-token parameter to Bearer. Reflected predictor/product responses use the existing redactor.
2. `models/shipment.py` and `controllers/main.py`: validate the complete selection before identity/refresh/network; fixed label endpoint, private HTTP downloads, no download-triggered commercial import.
3. `models/meli_link_cleanup.py`: prepared, narrowly scoped cleanup; no install/upgrade hook.
4. `tests/test_p0_token_label_image.py`: source-bound synthetic evidence, **not ORM/HTTP proof**. `tests/test_p0_native_security.py`: registered native Odoo19/PostgreSQL acceptance, pending actual CI execution.

## Compatibility and conservative boundaries

- Individual printing retains existing `pdf_file`, `pdf_filename`, `pdf_link` and optional in-memory JPG preview fields. Only changed values are written; label HTTP GET itself performs no shipment/order writes. Identity/expiry checks may perform the existing necessary OAuth refresh, not a new token protocol.
- Batch/report/automatic results retain their URL-list shape where consumed by automatic printing. URLs now target `/meli/labels/<local IDs>?mode=...`, require an Odoo session, and never contain provider credentials. Report data uses `batches`, not `by_token`; legacy token-keyed grouping is intentionally removed. An external print agent must use its authorized Odoo session, not an anonymous provider link. No arbitrary URL, model or method is accepted.
- Each label request contains at most 50 IDs. A 101-shipment selection produces 50/50/1 local links. No mixed company/seller operation is admitted, including when the user can select multiple companies.
- Label authorization checks native shipment/order access, active allowed company, configured Odoo seller user, stored remote seller identity, matching shipping IDs and any recorded sender. Missing/contradictory ownership fails closed before OAuth. The existing trusted identity snapshot must match the account after `get_new_instance`.
- Supported logistics: ME2 `cross_docking`, `xd_drop_off`, `drop_off`; status `ready_to_ship`, substatus `printed` or explicitly included `ready_to_print`. Fulfillment, shipped, pending and unknown logistics are not printable through this endpoint. PDF, provider ZPL2 ZIP, plain ZPL and extracted ZPL text remain supported. ZIP content and response size are bounded; JSON/errors/truncated labels/redirects are rejected with neutral errors.
- The existing shipment/company record rules remain unchanged. No global ACL or credential-facade privilege is granted. No `sudo()` is introduced. This correction does not certify uninstalled account extensions or unproved historical ownership; those records are rejected rather than guessed.
- Public image reads still use native permissions without elevation. A publication flag alone never overrides an ACL denial. Template images can serve its variants; a variant-specific image cannot serve a sibling variant, even on the same template. Private/foreign/missing/unrelated resources return the same 404. Public source-URL publication and multipart uploads are retained subject to native website access; native tests must establish their effective behavior.

## Storage inventory

| Location | Classification and action |
| --- | --- |
| Product `meli_permalink_api` | Non-stored computed field; no column migration invented. Correct the producer and reload code. |
| Shipment `pdf_link` | Stored; known MercadoLibre item/label URLs lose only the `access_token` query parameter. Newly generated values are local authenticated URLs. Cleanup alone leaves the old credential-free URL pointing at the protected provider API; regenerate via authorized Print/local binary download to restore usable download links. |
| Print wizard `full_links` | Transient model, but database-persisted until vacuum. Known legacy JSON grouping discards credential keys and preserves non-secret group values. |
| `meli.warning.message_html`, `message`, `copy_error` | Transient but database-persisted. Remove credentials only from known connector API URLs; preserve other message content. |
| Binary attachments, previews and filestore | Retained; no mass deletion or rewrite. Native access/ownership still applies. |
| Odoo `/web/content?...access_token=` capabilities | Deliberately untouched: these are unrelated attachment capabilities, not MercadoLibre OAuth credentials. |
| Logs, backups, browser history, exports, old notifications or copied messages | Not certified clean by this operation. Investigate separately under explicit real-data authorization, without printing secrets. |

## Prepared administrative operation — NOT EXECUTED

No persistent database was inspected or changed. Choose an explicit administrator operation, **not an automatic migration**, because remediation must be scoped, reviewed and separately authorized. There are no schema, ACL, view or manifest changes in this correction, so the new code itself requires a rebuild/restart, not an inferred module upgrade. A module upgrade does **not** run this cleanup. If later integration adds declarative/schema changes, classify those separately and require the appropriate upgrade proof.

Before any real write:

1. Obtain separate authorization for the exact environment/database, administrator identity, company scope and cleanup write. Confirm accepted connector SHA, backup and rollback plan. Disable unrelated commercial jobs through the approved operational procedure; do not enable cron or access live OAuth for this check.
2. On an isolated restored database with network disabled, run as the authorized administrator:

   ```python
   result = env['mercadolibre.shipment'].cleanup_legacy_label_links(dry_run=True)
   ```

   Only aggregate counts are returned. Do not print record values, tokens or headers. Review the known-field scope and validate representative synthetic/approved records without exposing values. Transient-model access remains native; the operation does not elevate to enumerate other users' wizard rows. A zero count does not certify inaccessible or already vacuumed history.
3. After explicit write approval, execute the same operation with `dry_run=False` in a transaction under the approved backup/rollback procedure. There is no implicit commit. Check only aggregate dry-run counts afterwards: targeted changed-record counts should be zero. Repeating the cleanup must make no changes. Preserve unrelated URLs, attachments and commercial/fiscal history.
4. Verify record rules and functional downloads with a real authorized user in the selected deployed database. A green deployment or source tree is not proof of loaded data/rules. Where a required module upgrade remains pending, report exactly `CODE DEPLOYED — DB UPGRADE PENDING`.

Historical logs/backups are not removed. No real credential exposure has been established in this local task. If a separately authorized investigation establishes exposure, report scope without secret values and propose credential rotation as a distinct operation; never rotate automatically.

## Verification and rollback

Exact local commands and measured RED/GREEN counts live in `odd/tasks/p0-token-label-image.md`. Local source-bound tests intercept transport and do not certify actual ACLs, PostgreSQL, native HTTP registration or deployed behavior. The existing PR CI retains its fresh Odoo19/PostgreSQL16 installation and full module test command; a separate step explicitly discovers the standalone security cohorts. Native tests, CI and real cleanup remain pending until their actual results are read back.

Rollback the reviewed feature code/tests/docs as one coherent unit; do not restore exposed credentials into sanitized links. Preserve existing binaries/history. No Odoo pin changes, push/PR/merge, HOMO or production operation is part of this writer's work. Stage 2 waits for connector acceptance/merge and tests the exact Odoo/connector pair.

## Official references

- [Authentication and authorization](https://developers.mercadolibre.com.ar/es_ar/autenticacion-y-autorizacion)
- [Mercado Envios mode 2: labels and request limits](https://developers.mercadolibre.com.ar/es_ar/mercadoenvios-modo-2)
- [Access control and authorization](https://developers.mercadolibre.com.ar/es_ar/control-de-acceso-y-autorizacion)
- [Working with images](https://developers.mercadolibre.com.ar/es_ar/trabajar-con-imagenes)
- [Authorization and token recommendations](https://developers.mercadolibre.com.ar/es_ar/recomendaciones-de-autorizacion-y-token)
