# P0: token-free product links, shipping labels and authorized images

## Objective and authority

Prevent credentials from reaching browser URLs, RPC results, persisted links or diagnostics while preserving legitimate Odoo 19 product queries, individual/batch/automatic label downloads and public/private image behavior.

Authority: the full user mandate in attachment `8fb4c920-9831-4e7b-ae8a-409faf383b72/Texto pegado.txt`, reviewed before this tracker. Implementation and synthetic verification on a new connector branch are authorized. Persistent cleanup is PREPARED ONLY. No HOMO/production changes, real credentials, commercial API operations, cron activation, order confirmation, invoicing or stock changes are authorized.

## Verified baseline and preservation

- Repository: `JoaPeralta/meli_oerp`; integration target: `codex/meli-bulk-contract`.
- Base and current Odoo connector pin: `bd9c10c06321d80343a41b75a6aa2268ce43b355`. Parent verified the current integration head equals this base; no later accepted commits were found.
- Selected branch: `codex/p0-token-label-image`; linked root and resolved common directory verified against canonical `viarengo-meli_oerp/repo/.git`; clean before this document.
- Preserve all unrelated worktrees, branches and pending work, including unrelated PR68/69 lineage. Do not use `19.0` as an assumed current integration base or mass-update upstream.
- Parent owns Git operations, Engram mirroring, network delivery and the final review/readback. No application source write before the full task mirror is saved and read back.
- Preserve existing OAuth/refresh, company/seller identity, manual/automatic import, economic validation, A/B isolation, warehouse/stock and fiscal guards. No global ACL changes, role grants or `sudo()` to bypass functional access.

## Confirmed problem and reuse boundaries

- `models/product.py`: predictor URL862 includes a token and is logged863; `meli_permalink_api`3409 exposes a credential-bearing URL through product URL widgets. This computed field does not declare `store=True`; do not invent a persisted-column cleanup requirement.
- `models/shipment.py`: `pdf_link` is persistent; print-wizard `full_links`, token-keyed grouping, HTML messages and auto-print results expose credentials. Reuse existing binary label/preview fields and authorized download behavior, not an arbitrary-URL proxy.
- Shipment `company_id` derives from its order; `seller_id` is an Odoo user, not the remote seller. Validate every shipment, related order, company and remote account before client/network work; refuse mixed or unproved ownership.
- `shipment_print` currently calls `update()`. A secure label download must not silently import/update commercial data or bypass the existing technical actor guard.
- `get_new_instance(company)` remains the OAuth/identity/refresh boundary. Generic SDK/NoSDK `get()` returns a JSON-oriented client result, not a guaranteed binary response; reuse existing authenticated backend patterns without inventing another OAuth client.
- `controllers/main.py`: public image routes have no `sudo()`, but do not explicitly bind `image_id` to `product_id`. Anonymous exploitation is NOT PROVEN. Apply native resource/publication checks and preserve legitimate website or MercadoLibre `source` access and multipart uploads.

## Stable tasks and acceptance

- [ ] **A — Products and predictor:** make API links, predictor paths, returned values and diagnostics credential-free; use existing server-side Bearer authentication where needed and preserve legitimate product query behavior. Observe causal failing token/URL/log tests before the minimal correction; retain OAuth regressions.
- [ ] **B — All label flows:** authorize each shipment/company/seller/account before fetching; use fixed-endpoint backend Bearer and private authorized binary downloads. Preserve individual, batch and automatic flows, PDF/ZPL/ZPL2 formats and printable-state behavior. Split requests at the documented maximum of 50 shipments; no mixed company/seller operation, arbitrary URL, token-keyed exposed dictionary or download-triggered commercial update. Cover denied ownership/access before network, provider failures/incomplete responses, safe errors and idempotence/no unnecessary writes.
- [ ] **C — Image authorization:** prove actual public and authenticated access using native ORM/HTTP behavior; bind image to the specified variant/template/product and apply company/access rules. Permit anonymous access only for a legitimately public resource, refuse private/unrelated/missing/ambiguous resources conservatively, avoid identifier disclosure and preserve public `source` and multipart compatibility. No blanket login requirement or indiscriminate elevation.
- [ ] **D — Prepared targeted sanitation:** inventory actual persistent/transient fields, wizard results and stored messages; prepare explicit administrator-only dry-run and idempotent targeted cleanup with value-safe counts and verification. Do not execute persistent writes or auto-run cleanup on install/upgrade. Preserve legitimate URLs/data/attachments/filestore and all commercial/fiscal history. Classify restart versus module upgrade versus separately authorized data operation; document residual historical log/backup exposure and a separate rotation proposal only if actual exposure is established.
- [ ] **E — Regression, native proof and review delivery:** run new causal RED/GREEN and relevant existing connector regressions; fresh disposable Odoo19/PostgreSQL tests with synthetic credentials and mocked external transport, effective public/normal-user access and exact discovery/counts. Preserve OAuth, imports, economics, A/B and stock contracts; independently review the complete candidate and record exact SHAs/modes/results/limits. Parent may commit, push and open one connector PR to the integration branch; NEVER merge without the user's approval. Odoo stage 2 stays pending until connector acceptance and merge.

Checkboxes close only on observed applicable evidence, never because source exists or CI was skipped. Current A-D status: implemented/prepared locally, **not fully closed**: native ORM/HTTP acceptance and final reviewed work-unit commit are pending. E remains partial. No persistent cleanup or operational effectiveness is claimed.

## TDD and exact verification routes

**TDD ON**, source: explicit user mandate section 6 requiring tests that fail on the vulnerable behavior and pass after correction. Observe RED -> GREEN -> REFACTOR per causal unit; an environment/fixture failure is not security RED. No workflow inferred from test presence.

Run from the selected connector root, using only directly relevant cohorts:

```powershell
& 'C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -I -B -X utf8 -m unittest discover -s tests -p '<cohort>' -v
```

Existing reusable cohorts: `test_image_upload_security.py`, `test_payment_token_security.py` and `test_shipment_format.py`; native cases include `test_download_saveas_removed.py`, `test_image_download_timeout.py`, `test_no_token_secrets_in_logs.py` and `test_token_expiry_metadata.py`. Select additional existing OAuth/HTTP regressions by the actual changed seam. Plain `unittest.TestCase` discovery must be reported separately from native Odoo tests; do not count omitted classes as passed.

Known baseline concern: the payment AST fixture may omit `payment_observations` now used by the importer. Establish its actual baseline and repair only a directly necessary fixture; any such setup error is NOT P0 RED and does not authorize weakening the product oracle.

Existing `.github/workflows/ci.yml` runs on PRs targeting `codex/meli-bulk-contract` with Odoo19 and PostgreSQL16; it has no manual dispatch. Its fresh disposable command is:

```sh
odoo -c /etc/odoo/odoo.conf -d meli_oerp_test_pr2 \
  --db_host=db --db_user=odoo --db_password=odoo \
  -i meli_oerp --test-enable --test-tags=/meli_oerp \
  --without-demo=all --log-level=test --stop-after-init
```

The database/password above belong only to that disposable CI service, not HOMO. Mock every relevant external connection; a workflow comment is not a measured network fence. No usable local Odoo/PostgreSQL runtime has been established; `wsl.exe` presence alone is not runtime proof. Native RED/GREEN remains NOT_RUN until actual execution and readback.

## Official API constraints

Parent consulted these five official DevSite pages live before implementation; this document records that evidence, not a new API call:

- [Authentication and authorization](https://developers.mercadolibre.com.ar/es_ar/autenticacion-y-autorizacion): authenticate protected server requests with Bearer; preserve supported refresh behavior.
- [Mercado Envios mode 2](https://developers.mercadolibre.com.ar/es_ar/mercadoenvios-modo-2): label PDF/ZPL2 formats, printable shipment conditions and at most 50 shipment IDs per request. Apply format/state restrictions, not only the batch count.
- [Access control and authorization](https://developers.mercadolibre.com.ar/es_ar/control-de-acceso-y-autorizacion): backend resource ownership/access validation; possession of an ID or visible button is not authorization.
- [Working with images](https://developers.mercadolibre.com.ar/es_ar/trabajar-con-imagenes): preserve supported image upload/source behavior; public source retrieval cannot depend on an Odoo login.
- [Authorization and token recommendations](https://developers.mercadolibre.com.ar/es_ar/recomendaciones-de-autorizacion-y-token): keep tokens confidential and out of browser-facing links/diagnostics.

## Delivery, rollback and evidence

- Delivery strategy: **single-pr**, explicitly requested. Forecast: approximately 1,200-1,800 authored changed lines including focused tests/docs; actual count pending. The units are cohesive security behavior with corresponding proof. 400 lines is advisory in verified VIARENGO scope, not permission to omit tests, minify, split artificially or ask a size-only approval question. Actual CI/hooks remain binding.
- Future work-unit commits: Conventional Commits, parent-verified JoaPeralta identity; no AI attribution or Co-Authored-By. Commit/push/PR evidence is currently NOT_RUN. Repository issues are disabled and no approved issue record or absent PR template is invented.
- Parent reports effective RDD ON/global on this root; candidate risk has NOT been assessed. This is neither review consent nor approval/receipt; no automatic grant or lifecycle execution is authorized by this tracker.
- Rollback: revert only this feature's reviewed behavior/tests/docs; retain credential-free sanitized values and existing binary/history data. Do not restore leaked tokens or execute persistent rollback/migrations without separate authority.
- Stage 2: after accepted connector merge, obtain the final merge SHA and create a separate Odoo pin/contract PR; test the exact pair and classify rebuild/restart, module upgrade and any separately authorized sanitation. No Odoo pin/source changes now.
- Evidence ledger: measured local cohort results are below; native fresh/HTTP/ORM NOT_RUN (7 registered native tests); independent review NOT_RUN; CI/push/PR/merge NOT_RUN; persistent cleanup NOT_RUN; no deployed runtime acceptance claimed.

## Implementation and evidence checkpoint

A-D form one coherent rollback/review candidate: protect browser-facing links and their authenticated binary replacement, prove image ownership, and prepare cleanup for the old producers. Files are `models/product.py`, `models/shipment.py`, `controllers/main.py`, `models/orders.py` (one discovered final-payment lookup only), new `models/meli_link_cleanup.py`, focused tests/registration, CI discovery and `P0_SECURITY.md`. No economic logic, ACL, schema, manifest, Odoo pin or commercial mutation was changed.

- **A:** credential-free API permalink; predictor uses existing client parameters/Bearer, no raw URL/result logging; existing redactor removes reflected credentials. Multipart production methods unchanged.
- **B:** fixed authenticated local label route, complete shipment/order/company/configured seller/remote seller/sender preflight before existing OAuth capability; require current trusted identity snapshot; PDF/ZPL2 ZIP/plain/extracted text, bounded response/ZIP, max50 and 50/50/1 splitting; neutral errors; no arbitrary URL; batch/report/auto links no tokens. Individual method preserves changed-only binary/preview fields and no longer calls commercial `update()`.
- **C:** native read/company checks and publication for public/portal callers; bind template images and variant images correctly, including sibling-negative; same 404 for missing/denied. No elevation introduced. Anonymous exploit on baseline remains UNPROVEN: local causal test proves missing binding with a synthetic authorized-read boundary, not anonymous native ACL access.
- **D:** explicit `cleanup_legacy_label_links(dry_run=True)` administrator method, no autorun/commit, aggregate counts only; targeted known URL parameters and legacy JSON keys, unrelated portal capabilities/data preserved. Transient rows may be inaccessible under their native owner rules: zero visible count does not certify historical cleanup. Details and real-write gates in `P0_SECURITY.md`.
- Additional directly related defect: `_ensure_payment_shipping_amounts` used `requests.get` with token query params and raw exception logging. Changed only authentication transport to Bearer and error metadata; preserved economic amount/status/write behavior.

### Observed RED (actual production methods, synthetic boundaries)

All commands ran from this linked root using:

```powershell
& 'C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -I -B -X utf8 -m unittest discover -s tests -p 'test_p0_token_label_image.py' -v
```

1. Initial 3-test cohort, BEFORE application source writes: **3 tests, 3 causal failures**. Actual product method exposed synthetic token in `meli_permalink_api`; actual single-label method returned token and called `update()`; actual image controller returned unrelated private bytes under a synthetic readable ORM boundary. No fixture/environment exception counted as RED.
2. After those fixes: **3/3 GREEN**.
3. Added final-payment actual-method test BEFORE its production correction: **4 tests, 1 causal failure**, proving `params=access_token=...` on the real requests call. After minimal Bearer/safe-error correction: **4/4 GREEN**.
4. Expanded source-bound tests for all corrected seams: final measured **21/21 GREEN** before the final bounded input/dry-run-query refinements; final rerun after those refinements also **21/21 GREEN**, 8.947s. These are not native ORM/HTTP results.

### Relevant existing regressions and fixture repair

Each command uses the exact Python prefix above and `-m unittest discover -s tests -p '<cohort>' -v`:

| Cohort | Observed result |
| --- | --- |
| `test_p0_token_label_image.py` | 21 passed; source-bound synthetic methods, no native runtime |
| `test_image_upload_security.py` | 17 passed; actual existing SDK/NoSDK multipart consumers |
| `test_payment_token_security.py` | baseline 6 run: 1 passed, 5 `NameError: economics` setup errors; repaired fixture/seam then 6 passed |
| `test_shipment_format.py` | 33 passed; existing format/header regression |
| `test_meli_economic_import.py` | 10 passed; accepted pre-write acquisition and no unsafe commercial reachability |

Total final measured relevant cohort outcomes: **87 passed, 0 skipped**, with initial RED/baseline failures retained separately above. The payment test had fallen behind the accepted economics implementation: it compiled only the metadata block, omitting `economics`, `money` and `payment_observations`. Its synthetic fixture now invokes the actual existing `_meli_fetch_payment_observation` method before that block; no production financial logic changed, and full response/fees/status/amount assertions remain. Obsolete Content-type and exception-log assertions were adapted to the accepted acquisition seam (Bearer, no URL/params credential, no raw diagnostic, unchanged financial assertions). The image inventory oracle now tracks exact path/operation/count, not fragile line offsets; all three multipart production methods remain covered.

Existing OAuth expiry/refresh-validation functions are executed as two focused source-bound regression tests inside the new cohort. Existing native OAuth/HTTP/credential/refresh cases remain registered in the preserved full native module CI, not falsely counted locally.

### Structural and unavailable proof

- Python AST parse: 10 changed Python files parsed successfully (syntax only); synthetic PNG decoded successfully with available Pillow.
- Existing working source files were CRLF (`models/shipment.py` baseline index was mixed); preserved working line endings. Default `git diff --check` flags CR-at-EOL; `git -c core.whitespace=cr-at-eol diff --check` passed without changing configuration. Parent owns final staged-blob normalization/identity verification.
- New native `tests/test_p0_native_security.py`: **7 tests registered**, including actual public/normal-user HttpCase, native ACL/company/remote-seller denials before OAuth, credential facade denial, binary GET no writes, individual/batch/auto PDF/ZPL and prepared cleanup on synthetic rows. **NOT_RUN**: Odoo/docker unavailable; WSL not installed; PostgreSQL client presence is not an owned disposable server. No services installed or persistent DB accessed.
- CI adds three explicit standalone discovery commands for ordinary unittest cohorts, retains native `test_shipment_format` and all existing native cases under unchanged Odoo19/PG16 full module install command. Running standalone `test_shipment_format` inside installed Odoo could select TransactionCase without a configured test registry, so its CI proof stays native. CI itself **NOT_RUN** until parent-authorized PR trigger/readback.
- Restart classification: source-only/no schema/ACL/view/manifest change; rebuild/restart for code. Cleanup is a separately authorized administrative data operation, not a migration hook or implicit upgrade. Never claim real DB effectiveness; no deployment performed.
- No Git stage/commit/push/PR/merge, native RDD lifecycle, credential operation, cron, HOMO, production, stock/order/invoice write or real API call was executed by the writer. Parent owns final candidate readback, independent verification/review, commit identity and mirror.

## Bounded independent-verifier correction round (finding 8309)

**Finding and root cause:** the first candidate unconditionally read `company.mercadolibre_shipment_print_guide_mode` in batch and individual printing. That field is supplied only by an optional extension and is not declared by this base module; its installed base default is the existing `models/versions.py` constant (`pdf`). The initial native fixture also tried to write that undeclared company field. This was a candidate-caused compatibility defect, not evidence that the base schema should be expanded.

**Observed causal RED before this correction:** added two actual-production-method tests with the real base-company shape (`_fields={}`; no optional mode attribute). Ran the same exact command:

```powershell
& 'C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -I -B -X utf8 -m unittest discover -s tests -p 'test_p0_token_label_image.py' -v
```

Result: **23 tests run, 21 passed, 2 errors**, both causal `AttributeError` in actual `_get_shipment_labels_data` (line210) and `shipment_print` (line2097), before their label transport. These errors are the observed base-module defect, unlike the earlier stale payment-fixture setup errors.

**Bounded correction:** one private `_label_print_mode(config)` resolver guards the optional field using `config._fields`, otherwise reusing the existing base constant. Both label paths share it. No model field/schema/ACL/dependency added. Native fixtures no longer write any optional field; their format variants patch only the existing module default. Documentation now explicitly states that cleanup strips the old query credential but leaves a protected provider API URL; authorized Print/local-binary regeneration is required to restore a usable link. Cleanup scope/behavior itself was not expanded.

**Observed GREEN and regression after correction:**

| Exact cohort (same Python prefix/runner above) | Result |
| --- | --- |
| `test_p0_token_label_image.py` | **23/23 passed**, 12.268s |
| `test_image_upload_security.py` | **17/17 passed**, 5.198s |
| `test_payment_token_security.py` | **6/6 passed**, 0.967s |
| `test_shipment_format.py` | **33/33 passed**, 0.141s |
| `test_meli_economic_import.py` | **10/10 passed**, 9.547s |

Current relevant local result: **89 passed, 0 skipped**. Earlier 87-pass evidence remains the pre-correction snapshot, not overwritten or mislabeled. Changed-round Python syntax: **3/3 parsed**; structural AST check confirms no native optional-field write remains. `git -c core.whitespace=cr-at-eol diff --check` passed. Seven registered native ORM/HTTP tests remain **NOT_RUN**; fixture/source inspection is not native acceptance. Native CI, final work-unit commit, native RDD and remote delivery remain pending and parent-owned. This is the single bounded correction round; same independent verifier should perform its focused recheck.

## Parent verification checkpoint

The fresh independent verifier reproduced the optional-field defect, then rechecked the one bounded correction: both missing-extension actual-method cases pass; native fixture AST has seven registered tests and no undeclared-field writes. Its final focused command ran `test_p0_token_label_image.py`: **23 passed, 0 failed, 0 skipped**, 15.436s. The other four cohorts were independently green before correction and were not rerun by that verifier afterwards; the writer separately reran all five cohorts after correction: **89 passed, 0 skipped**. Do not combine these executions into additional distinct tests.

The parent also directly spot-checked the pre-correction 21-test cohort: **21 passed**, 11.634s. Final staged normalization/identity checks, native RDD consent, work-unit commit, PR-triggered native CI and delivery remain pending. No local runtime or deployed effectiveness is claimed.

## Immediate next step

Parent reads back this full document and candidate, refreshes/readbacks the Engram mirror, and requests the same independent verifier's focused correction recheck. Retain A-D unchecked until actual native proof/final commit acceptance; E stays partial. Follow existing native RDD consent/bindings without granting on the user's behalf. One connector PR only, never merge; stage2 remains blocked on connector acceptance/merge.
