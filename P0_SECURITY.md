# P0 security correction: deployment and prepared cleanup

The connector now uses token-free product links and local, authenticated label URLs. Label credentials stay in the existing server-side OAuth capability. Image requests must prove product/variant/template ownership and effective read access; anonymous and portal requests additionally require website publication.

## Review path

1. `models/product.py`: credential-free internal API resource URL and predictor parameters; existing SDK translates the access-token parameter to Bearer. Reflected responses use the existing redactor. `views/product_view.xml` removes the two unusable protected API URL widgets and retains the existing public `meli_permalink` Link; no new proxy or commercial import action.
2. `models/shipment.py` and `controllers/main.py`: validate the complete selection before identity/refresh/network; fixed label endpoint, private HTTP downloads, no download-triggered commercial import.
3. `models/meli_link_cleanup.py`: prepared, narrowly scoped cleanup; no install/upgrade hook.
4. `tests/test_p0_token_label_image.py`: source-bound synthetic evidence, **not ORM/HTTP proof**. The previous seven native methods passed on the prior source commit. The human-reviewed compatibility correction expands this to **11 registered native methods**, including default-wizard and returned-auto-URL HTTP downloads and effective product views; new-candidate native CI is **PENDING**, not covered by the older result.

## Compatibility and conservative boundaries

- Individual printing retains existing `pdf_file`, `pdf_filename`, `pdf_link` and optional in-memory JPG preview fields. Only changed values are written; label HTTP GET itself performs no shipment/order writes. Identity/expiry checks may perform the existing necessary OAuth refresh, not a new token protocol.
- Batch/report/automatic results retain their URL-list shape where consumed by automatic printing. URLs now target `/meli/labels/<local IDs>?mode=...`, require an Odoo session, and never contain provider credentials. Report data uses `batches`, not `by_token`; legacy token-keyed grouping is intentionally removed. An external print agent must use its authorized Odoo session, not an anonymous provider link. No arbitrary URL, model or method is accepted.
- Each label request contains at most 50 IDs. A 101-shipment selection produces 50/50/1 local links. No mixed company/seller operation is admitted, including when the user can select multiple companies.
- Label authorization checks native shipment/order access, active allowed company, configured Odoo seller user, stored remote seller identity, matching shipping IDs and any recorded sender. Missing/contradictory ownership fails closed before OAuth. The existing trusted identity snapshot must match the account after `get_new_instance`.
- Supported logistics: ME2 `cross_docking`, `xd_drop_off`, `drop_off`, **`self_service` (Flex)**; status `ready_to_ship`, substatus `ready_to_print` or `printed`. The documented availability predicate is separate from automatic selection. Fulfillment, shipped, pending and unknown logistics are not printable through this endpoint. PDF, provider ZPL2 ZIP, plain ZPL and extracted ZPL text remain supported. ZIP content and response size are bounded; JSON/errors/truncated labels/redirects are rejected with neutral errors.
- Manual Print (including a default wizard with `include_ready_to_print=False`) and individual printing include both valid first prints and reprints. Automatic selection preserves its legacy behavior: unprinted `ready_to_print` by default; include `printed` only when the legacy `include_ready_to_print` argument is true. The misleading legacy argument name is retained for compatibility; no global inversion of that flag or authorization bypass is used.
- The existing shipment/company record rules remain unchanged. No global ACL or credential-facade privilege is granted. No `sudo()` is introduced. This correction does not certify uninstalled account extensions or unproved historical ownership; those records are rejected rather than guessed.
- Public image reads still use native permissions without elevation. A publication flag alone never overrides an ACL denial. Template images can serve its variants; a variant-specific image cannot serve a sibling variant, even on the same template. Private/foreign/missing/unrelated resources return the same 404. Public source-URL publication and multipart uploads are retained subject to native website access. Source-commit disposable native HTTP/ORM tests passed; this does not establish behavior in a deployed database.

## Automatic consumer contract and unavailable integration

The connector returns **relative URLs**, not anonymous MercadoLibre download links. A consumer must resolve each URL against its **configured trusted Odoo origin** and send its existing **authorized Odoo session**, including the permitted active company. A separate service, desktop print agent or cross-origin client does not inherit the browser session automatically. No new OAuth protocol, public capability token or arbitrary-URL proxy is provided here.

Before printing, the consumer must handle login-required responses (redirect or 401/403) without treating login HTML as label data, and handle a neutral 404 as denied/unavailable rather than retrying anonymously. Validate the expected PDF or ZPL/ZIP content; use existing approved session renewal/login handling. Never put provider tokens in URLs, pass them to JavaScript/RPC, or forward Odoo cookies/provider credentials to another origin.

A bounded scan of readable connector `.py`, `.js` and `.xml` sources found only the wrapper/helper and local label route; the same-name/URL scan of the canonical local Odoo source found **no actual automatic print consumer**. No uninstalled external extension/service was inspected or changed, and historical worktrees with unavailable access are not certified. Native tests now exercise `_get_labels_urls_auto_print()` output through real authenticated local HTTP for PDF/ZPL, with anonymous/unauthorized denial; these new tests are **PENDING CI**. Even a passing connector HTTP test does not certify an absent external consumer. Its origin/session/login-response integration remains an explicit external dependency requiring separate evidence/authorization, not an assumed compatibility claim.

## Storage inventory

| Location | Classification and action |
| --- | --- |
| Product `meli_permalink_api` | Non-stored, token-free internal resource URL; no column migration. The protected API URL is no longer offered as a browser widget. Existing public `meli_permalink` remains available without an OAuth call. |
| Shipment `pdf_link` | Stored; known MercadoLibre item/label URLs lose only the `access_token` query parameter. Newly generated values are local authenticated URLs. Cleanup alone leaves the old credential-free URL pointing at the protected provider API; regenerate via authorized Print/local binary download to restore usable download links. |
| Print wizard `full_links` | Transient model, but database-persisted until vacuum. Known legacy JSON grouping discards credential keys and preserves non-secret group values. |
| `meli.warning.message_html`, `message`, `copy_error` | Transient but database-persisted. Remove credentials only from known connector API URLs; preserve other message content. |
| Binary attachments, previews and filestore | Retained; no mass deletion or rewrite. Native access/ownership still applies. |
| Odoo `/web/content?...access_token=` capabilities | Deliberately untouched: these are unrelated attachment capabilities, not MercadoLibre OAuth credentials. |
| Logs, backups, browser history, exports, old notifications or copied messages | Not certified clean by this operation. Investigate separately under explicit real-data authorization, without printing secrets. |

## Prepared administrative operation — PERSISTENT CLEANUP NOT EXECUTED

No persistent database was inspected or changed. Choose an explicit administrator operation, **not an automatic migration**, because remediation must be scoped, reviewed and separately authorized. **MODULE UPGRADE REQUIRED** for the compatibility correction: `views/product_view.xml` removes two protected API URL widgets from existing `ir.ui.view` records. Rebuild/restart alone does not update installed database views; an authorized upgrade of `meli_oerp` is required, followed by real-user combined-view verification. The existing public `meli_permalink` link remains. There is no new schema, ACL or dependency. A module upgrade does **not** run the prepared cleanup; persistent cleanup still requires its own separate authorization.

The XML upgrade acceptance requires `meli_oerp`'s installed module state, the `meli_oerp.product_tpl_ml_form_view` and `meli_oerp.product_normal_ml_form_view` records, and the effective combined views for a real authorized catalog user: existing public Link present, protected API widget absent. Source XML or a green deploy is not this database proof. No upgrade has been executed by this correction.

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

Exact local commands and measured RED/GREEN counts live in `odd/tasks/p0-token-label-image.md`. Local source-bound tests intercept transport and do not certify actual ACLs, PostgreSQL, native HTTP registration or deployed behavior. The existing PR CI retains its fresh Odoo19/PostgreSQL16 installation and full module test command; a separate step explicitly discovers the standalone security cohorts. Actual source-commit native CI evidence is recorded below. Persistent cleanup remains unexecuted, and CI for the subsequent documentation HEAD must still be read back separately.

Rollback the reviewed feature code/tests/docs as one coherent unit; do not restore exposed credentials into sanitized links. Preserve existing binaries/history. The parent published the source commit in draft PR77. No merge, Odoo pin change, persistent cleanup, HOMO or production operation has been performed. Stage 2 waits for connector acceptance/merge and tests the exact Odoo/connector pair.

## Prior candidate publication and measured acceptance

The evidence below is historical and belongs to the earlier security candidate. It does **not** certify the new compatibility edits, their XML upgrade or an absent automatic consumer. New-candidate verification/publication remains parent-owned and pending.

- **Source:** [`6875a993d9d138b628b71e045c1e78c1a11b72fd`](https://github.com/JoaPeralta/meli_oerp/commit/6875a993d9d138b628b71e045c1e78c1a11b72fd), parent `bd9c10c06321d80343a41b75a6aa2268ce43b355`, exact tree `099c5e80c7372075f75004088c8dede01ae90b9a`. Author and committer: `JoaPeralta <peralta.joaquin@hotmail.com>`.
- **Publication:** [Draft PR77](https://github.com/JoaPeralta/meli_oerp/pull/77) targets `codex/meli-bulk-contract`; **not merged**. Odoo stage2/pin changes remain pending connector acceptance and merge.
- **Independent local verification:** **89 passed, 0 failed, 0 skipped**, cohorts **23/17/6/33/10**. Preserve earlier RED and fixture failures in the task ledger rather than rewriting them.
- **Actual source-commit CI:** [Run42085481681, job127449301886](https://github.com/JoaPeralta/meli_oerp/actions/runs/42085481681/job/127449301886) **SUCCESS**. Standalone cohorts **23 + 17 + 6** passed. Odoo19/PostgreSQL16 logs showed all **seven P0 native methods** starting, **zero P0 skips**, and overall **`0 failed, 0 error(s) of 456 tests`**.
- **One global skip:** `TestLegacyOrderNotificationResults.test_technical_actor_still_cannot_process_legacy_outcomes`; its optional bridge actor was not installed. This is not PASS and does not certify the optional bridge integration. Statistics value **568 includes auxiliary counts**, not 568 passing test cases; do not add separate local/CI executions into a distinct-test total.
- **Review authority:** the user declined extra native review; the exact decline ran once and validated `action: declined`, `consent: declined_this_candidate`. No native receipt or approval was produced, and the review switch was unchanged.

Cleanup apply acceptance used **synthetic disposable CI data only**. No persistent cleanup, deployment, HOMO/production operation or real-credential rotation was executed. Passing CI does not prove deployed database effectiveness or an upgrade. Historical protected API links still need authorized Print/local-binary regeneration after sanitation, as described above.

The successful CI above belongs only to the identified historical source commit. The later reviewed baseline was `4f95ad35dc594da236afe714be707dbae2154368`. The new compatibility correction requires its own local regression evidence, independent verification, same-draft-PR commit/push and final-HEAD native CI before acceptance; none is inherited from an earlier green run. Merge, Odoo pin changes and deployed upgrade/cleanup remain separate decisions.

## Official references

- [Authentication and authorization](https://developers.mercadolibre.com.ar/es_ar/autenticacion-y-autorizacion)
- [Mercado Envios mode 2: labels and request limits](https://developers.mercadolibre.com.ar/es_ar/mercadoenvios-modo-2)
- [Access control and authorization](https://developers.mercadolibre.com.ar/es_ar/control-de-acceso-y-autorizacion)
- [Working with images](https://developers.mercadolibre.com.ar/es_ar/trabajar-con-imagenes)
- [Authorization and token recommendations](https://developers.mercadolibre.com.ar/es_ar/recomendaciones-de-autorizacion-y-token)
