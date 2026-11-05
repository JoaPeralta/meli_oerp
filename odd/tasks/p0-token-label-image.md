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

- [x] **A — Products and predictor:** make API links, predictor paths, returned values and diagnostics credential-free; use existing server-side Bearer authentication where needed and preserve legitimate product query behavior. Observe causal failing token/URL/log tests before the minimal correction; retain OAuth regressions.
- [x] **B — All label flows:** authorize each shipment/company/seller/account before fetching; use fixed-endpoint backend Bearer and private authorized binary downloads. Preserve individual, batch and automatic flows, PDF/ZPL/ZPL2 formats and printable-state behavior. Split requests at the documented maximum of 50 shipments; no mixed company/seller operation, arbitrary URL, token-keyed exposed dictionary or download-triggered commercial update. Cover denied ownership/access before network, provider failures/incomplete responses, safe errors and idempotence/no unnecessary writes.
- [x] **C — Image authorization:** prove actual public and authenticated access using native ORM/HTTP behavior; bind image to the specified variant/template/product and apply company/access rules. Permit anonymous access only for a legitimately public resource, refuse private/unrelated/missing/ambiguous resources conservatively, avoid identifier disclosure and preserve public `source` and multipart compatibility. No blanket login requirement or indiscriminate elevation.
- [x] **D — Prepared targeted sanitation:** inventory actual persistent/transient fields, wizard results and stored messages; prepare explicit administrator-only dry-run and idempotent targeted cleanup with value-safe counts and verification. Do not execute persistent writes or auto-run cleanup on install/upgrade. Preserve legitimate URLs/data/attachments/filestore and all commercial/fiscal history. Classify restart versus module upgrade versus separately authorized data operation; document residual historical log/backup exposure and a separate rotation proposal only if actual exposure is established.
- [x] **E — Regression, native proof and review delivery:** run new causal RED/GREEN and relevant existing connector regressions; fresh disposable Odoo19/PostgreSQL tests with synthetic credentials and mocked external transport, effective public/normal-user access and exact discovery/counts. Preserve OAuth, imports, economics, A/B and stock contracts; independently review the complete candidate and record exact SHAs/modes/results/limits. Parent may commit, push and open one connector PR to the integration branch; NEVER merge without the user's approval. Odoo stage 2 stays pending until connector acceptance and merge.

Checkboxes close only on observed applicable evidence. A-D remain complete in their implemented/prepared scope; D never means persistent cleanup. Historical E publication is complete. **F/G and I are complete within committed-code/disposable-CI acceptance** on `ae0c2d5de0c6954664b66c6fb71457d430028a4c`. **H remains partial:** connector selection/session HTTP tests and dependency documentation are complete, but the real external automatic consumer is unlocated/unverified. This final passive evidence-document commit/HEAD CI is a separate parent publication checkpoint, not yet claimed PASS; no merge, deployed upgrade or outside integration is certified.

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

The database/password above belong only to that disposable CI service, not HOMO. Mock every relevant external connection; a workflow comment is not a measured network fence. No usable local Odoo/PostgreSQL runtime has been established; `wsl.exe` presence alone is not runtime proof. Local native RED remains NOT_RUN. Actual source-commit native GREEN was later observed in disposable CI; see the final publication evidence below.

## Official API constraints

Parent consulted these five official DevSite pages live before implementation; this document records that evidence, not a new API call:

- [Authentication and authorization](https://developers.mercadolibre.com.ar/es_ar/autenticacion-y-autorizacion): authenticate protected server requests with Bearer; preserve supported refresh behavior.
- [Mercado Envios mode 2](https://developers.mercadolibre.com.ar/es_ar/mercadoenvios-modo-2): label PDF/ZPL2 formats, printable shipment conditions and at most 50 shipment IDs per request. Apply format/state restrictions, not only the batch count.
- [Access control and authorization](https://developers.mercadolibre.com.ar/es_ar/control-de-acceso-y-autorizacion): backend resource ownership/access validation; possession of an ID or visible button is not authorization.
- [Working with images](https://developers.mercadolibre.com.ar/es_ar/trabajar-con-imagenes): preserve supported image upload/source behavior; public source retrieval cannot depend on an Odoo login.
- [Authorization and token recommendations](https://developers.mercadolibre.com.ar/es_ar/recomendaciones-de-autorizacion-y-token): keep tokens confidential and out of browser-facing links/diagnostics.

## Delivery, rollback and evidence

- Delivery strategy: **single-pr**, explicitly requested. Forecast: approximately 1,200-1,800 authored changed lines including focused tests/docs; actual count pending. The units are cohesive security behavior with corresponding proof. 400 lines is advisory in verified VIARENGO scope, not permission to omit tests, minify, split artificially or ask a size-only approval question. Actual CI/hooks remain binding.
- Work-unit source commit: `6875a993d9d138b628b71e045c1e78c1a11b72fd`, parent `bd9c10c06321d80343a41b75a6aa2268ce43b355`, tree `099c5e80c7372075f75004088c8dede01ae90b9a`; author and committer `JoaPeralta <peralta.joaquin@hotmail.com>`. Published in draft PR77; no merge. Conventional Commit, no AI attribution or Co-Authored-By. Repository issues are disabled and no approved issue record or absent PR template is invented.
- Native-review authority is candidate-scoped: the earlier security candidate was declined once; that decline was never reused. For the compatibility commit, assessment/status failed `pre_native` with Access denied on a temporary index file (`mutation_not_started`, `authority_not_evaluated`). No START, consent envelope, approval or receipt resulted; no ACL/config/permission bypass was attempted. Independent verification and explicitly authorized draft publication continued under ordinary policy, not native PASS.
- Rollback: revert only this feature's reviewed behavior/tests/docs; retain credential-free sanitized values and existing binary/history data. Do not restore leaked tokens or execute persistent rollback/migrations without separate authority.
- Stage 2: after accepted connector merge, obtain the final merge SHA and create a separate Odoo pin/contract PR; test the exact pair and classify rebuild/restart, module upgrade and any separately authorized sanitation. No Odoo pin/source changes now.
- Current evidence ledger: writer and independent local cohorts **94 passed, 0 failed, 0 skipped** (28/17/6/33/10); parent spot check **28 passed**, 12.252s. Compatibility commit CI succeeded: all **11 P0 native methods** observed, **0 P0 skips**, overall **0 failed, 0 errors of 460 tests**, exactly one documented optional-bridge global skip. Draft PR77 updated, not merged; this passive documentation HEAD remains a separate unverified checkpoint. Persistent cleanup, deployed upgrade, pin, HOMO/production and absent-consumer integration NOT_EXECUTED.

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

## Final source publication and CI evidence

This section supersedes pending-delivery/native-GREEN statements in the historical checkpoints above without rewriting their observations. Evidence was supplied and read back by the parent; this passive documentation update performs no remote operation or runtime test.

| Identity or boundary | Observed evidence |
| --- | --- |
| Source commit | [`6875a993d9d138b628b71e045c1e78c1a11b72fd`](https://github.com/JoaPeralta/meli_oerp/commit/6875a993d9d138b628b71e045c1e78c1a11b72fd) |
| Parent / exact source tree | `bd9c10c06321d80343a41b75a6aa2268ce43b355` / `099c5e80c7372075f75004088c8dede01ae90b9a` |
| Author and committer | `JoaPeralta <peralta.joaquin@hotmail.com>` |
| Publication | [Draft PR77](https://github.com/JoaPeralta/meli_oerp/pull/77), target `codex/meli-bulk-contract`; not merged |
| Source-commit CI | [Run42085481681, job127449301886](https://github.com/JoaPeralta/meli_oerp/actions/runs/42085481681/job/127449301886): **SUCCESS** |
| Final independent local rerun | **89 passed, 0 failed, 0 skipped**: P0 23, multipart 17, payment 6, shipment-format 33, economic-import 10 |
| Candidate extra native review | User declined; exact decline executed once and validated `action: declined`, `consent: declined_this_candidate`; **no receipt or approval**, switch unchanged |

### Actual disposable native acceptance

The source-commit CI used **Odoo19/PostgreSQL16**. Its explicit standalone cohorts passed **23 + 17 + 6**. The parent observed `Starting` lines for **all seven P0 native methods**, **zero P0 skips**, and the overall Odoo summary **`0 failed, 0 error(s) of 456 tests`**. These are actual native executions, not AST doubles or registered-but-unrun counts.

There was exactly one global skip: `TestLegacyOrderNotificationResults.test_technical_actor_still_cannot_process_legacy_outcomes`, because the optional bridge actor was not installed. The skip is **not PASS**, and this CI does **not certify the optional bridge integration**. The separate statistics value **568 includes auxiliary counts** and must not be described as 568 passing test cases. Local cohort counts and CI/native summaries are separate executions, not additional distinct tests to sum.

The cleanup apply test ran **only on synthetic disposable CI rows**. No persistent cleanup was executed. CI does not certify deployed database effectiveness, production/HOMO, live credentials, real API behavior or a module upgrade.

### Remaining publication boundary

This evidence belongs to source commit `6875a993d9d138b628b71e045c1e78c1a11b72fd`, not an unverified later HEAD. The parent still needs to mirror/read back this full task document, commit the two passive documentation updates and verify the new-HEAD CI rerun before closing E. No further functional test is required merely for writing these passive documents; structural readback and link/scope checks are the applicable local checks.

The PR remains **draft and unmerged**. No Odoo pin update, stage2 implementation, persistent cleanup, HOMO or production change occurred. Stage2 waits for explicit connector acceptance and merge; real cleanup requires its separate authorization, backup/rollback and operational proof.

## Immediate next step

Parent refreshes and reads back the full Engram task mirror, commits only these two publication-evidence documents, and verifies the new-HEAD CI rerun. Then reconcile E without treating draft publication or a candidate-scoped review decline as merge authority. Keep PR77 unmerged; Odoo stage2 and persistent cleanup remain outside this publication checkpoint.

## PR77 human compatibility review (2026-11-05)

Authority: the user reviewed final HEAD `4f95ad35dc594da236afe714be707dbae2154368` and authorized bounded corrections, causal RED/GREEN, relevant local tests, full native CI, commit and push to this same draft PR. No merge, Odoo pin, HOMO, production, live credentials, permission expansion or outside integration changes. The earlier final-HEAD CI succeeded (run42086894338); historical E publication was completed, with the optional bridge case explicitly skipped. Earlier native-review decline covered only the previous candidate, not new code.

Resume baseline: clean `codex/p0-token-label-image` at the reviewed SHA; verified common directory matches canonical connector. TDD remains ON by explicit human request. Runner remains the exact Python3.12 `-I -B -X utf8 -m unittest discover -s tests -p '<cohort>' -v` recorded above. Native Odoo/PostgreSQL proof uses existing disposable PR CI, not HOMO. Local unavailable-native evidence remains valid.

- [x] **F — Label compatibility:** causal tests for official ME2 `self_service` and rejected Fulfillment; restore default wizard first-print `ready_to_ship/ready_to_print` and preserve reprints without blindly inverting the distinct automatic-selection flag. Add actual native default wizard creation/button-to-download test with a normal authorized user. Official source: [Mercado Envios2](https://developers.mercadolibre.com.ar/es_ar/mercadoenvios-modo-2), fetched today, includes self_service and first/reprint states. Keep all account/access/Bearer/max50 constraints.
- [x] **G — Product browser link:** prove why a credential-free protected API URL is not a usable URL-widget action and reuse the existing public permalink or a minimal authenticated equivalent. Do not reuse an import action that mutates commercial data or expose credentials in URLs/JS/RPC. Declare XML/module upgrade impact if the correction changes a view.
- [ ] **H — Automatic consumer boundary:** identify real callers in the connector and authorized local related source, preserve legacy selection semantics, and exercise returned URLs through authenticated native HTTP (including unauthenticated/unauthorized denial). Record any unavailable extension/service, origin/session dependency and required external changes precisely; do not certify absent consumers or broaden their authority.
- [x] **I — Verification and same-PR publication:** run relevant local cohorts, independent scope/security regression verification, and full native CI on final HEAD; Conventional Commit(s) with JoaPeralta identity and no attribution. Keep PR77 draft. Record skipped/unavailable proof, actual SHAs, upgrade classification and external dependencies; no merge/pin/runtime operations.

One cohesive compatibility correction to the existing security unit, tests and docs together. Size remains advisory; no artificial chain/refactor. Rollback only the new bounded compatibility commit(s), never reintroduce URL credentials. Initial evidence: concrete default-report first-print regression confirmed by comparing bd9c10c; current native test passes True explicitly and misses the actual default wizard. API resource link utility not demonstrated; existing public permalink is available. No actual auto-print consumer found in initially inspected connector/Odoo roots; some historical related worktrees were unreadable, so consumer compatibility is not yet certified. Parent must mirror/read back this updated tracker before application source writes.


## Human compatibility correction implementation checkpoint

Baseline `4f95ad35dc594da236afe714be707dbae2154368`; same draft PR77 and verified linked root. Parent mirrored F-I before the first application edit. Earlier A-E implementation/publication evidence and all historical RED/failures above remain intact; they do not certify this new candidate.

### F — Implemented locally; new native acceptance pending

The actual eligibility predicate now includes official ME2 `self_service` (Flex) alongside `drop_off`, `xd_drop_off` and `cross_docking`, and still rejects Fulfillment, non-ME2, unavailable states and unknown logistics. The parent fetched the official [Mercado Envios2](https://developers.mercadolibre.com.ar/es_ar/mercadoenvios-modo-2) page on 2026-11-05 (page updated 2026-07-06) and supplied that constraint; this writer made no external API/documentation request.

Availability is separated from selection. Explicit manual Print (including the actual default false-valued wizard) and individual printing permit valid `ready_to_print` first prints and `printed` reprints. Automatic selection preserves the distinct legacy meaning: unprinted first prints by default, printed only when `include_ready_to_print=True`. No flag was blindly inverted globally. Complete selection authorization still precedes any client/network work; Bearer, neutral errors, identity/company/seller checks, max50 batches, private binaries and no commercial update remain unchanged.

### G — Existing public link retained; XML upgrade required

Removed exactly two `meli_permalink_api` URL-widget declarations from `views/product_view.xml`; the existing public `meli_permalink` Link remains in both views. The token-free resource URL can remain internal, but a protected API requiring Bearer is no longer offered as a browser action. No import/commercial action, authenticated proxy, OAuth protocol, RPC/JavaScript credential or permission expansion was introduced.

**MODULE UPGRADE REQUIRED:** these are installed `ir.ui.view` record changes. Rebuild/restart alone is insufficient; an authorized `meli_oerp` module upgrade and real-user effective-view readback are required. The new native test checks both effective combined product views and the existing public permalink on a disposable installed database; it has not run locally and does not prove a deployed database upgrade. No schema/ACL/dependency/migration hook changed, and no upgrade or persistent cleanup was executed.

### H — Connector contract exercised in tests; absent consumer not certified

The bounded `.py`/`.js`/`.xml` scan found only the connector wrapper/helper and local label route; no actual automatic-print caller was found in readable connector or canonical local Odoo sources. Unavailable historical worktrees and uninstalled external extensions/services remain uninspected. There is no authorized outside-consumer source change.

`P0_SECURITY.md` now states the required external contract: resolve relative URLs against the configured trusted Odoo origin, use an authorized Odoo session/active company, and handle login redirects/401/403 and neutral 404 without printing HTML or retrying anonymously. Cross-origin/separate agents do not inherit a browser session. Existing approved login/session handling must be integrated by that consumer; never forward provider tokens or Odoo cookies to another origin. This is a documented dependency, **not a fabricated overall compatibility claim**.

Four new native methods are registered in the existing native test file (11 total): Flex positive/Full negative, default normal-Sales-Manager wizard button -> produced link -> actual session HTTP download for first print and reprint, automatic helper result -> session HTTP PDF/ZPL with anonymous/unauthorized denial and default/reprint filtering, and effective product views/public link without OAuth. They intercept provider transport but retain actual local Odoo HTTP/ORM. **New-candidate native methods: NOT_RUN locally; final CI pending.** Older seven-test CI evidence remains historical, not proof for these edits or absent consumers.

### Observed RED before application correction

Exact command from this root:

```powershell
& 'C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -I -B -X utf8 -m unittest discover -s tests -p 'test_p0_token_label_image.py' -v
```

Result: **28 tests, 8 assertion failures, 0 errors**, 12.776s. Five new methods produced these failures (subtests counted separately by unittest): auto default/false selected the printed shipment instead of the unprinted shipment (2); actual default wizard report omitted the first-print link (1); Flex eligibility rejected `self_service` (1); default individual first print returned no download URL (1); both product views retained protected API widgets (2); documentation lacked the required upgrade declaration (1). The original 23 tests passed. No fixture/environment exception was counted as causal RED; the documentation assertion is acceptance metadata, not a runtime security exploit. Local native RED is unavailable and is not invented.

After the minimal application/view correction and upgrade declaration, the same command was **28/28 GREEN**, 12.236s. No broad refactor or additional framework was needed.

### Final relevant local GREEN

All five exact runner commands were rerun on the final application/test/XML bytes:

```powershell
& 'C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -I -B -X utf8 -m unittest discover -s tests -p 'test_p0_token_label_image.py' -v
& 'C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -I -B -X utf8 -m unittest discover -s tests -p 'test_image_upload_security.py' -v
& 'C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -I -B -X utf8 -m unittest discover -s tests -p 'test_payment_token_security.py' -v
& 'C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -I -B -X utf8 -m unittest discover -s tests -p 'test_shipment_format.py' -v
& 'C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -I -B -X utf8 -m unittest discover -s tests -p 'test_meli_economic_import.py' -v
```

| Cohort | Observed result |
| --- | --- |
| P0 source-bound security/compatibility | 28 passed, 0 failed, 0 skipped; 13.489s |
| Existing multipart uploads | 17 passed, 0 failed, 0 skipped; 4.858s |
| Existing payment-token security | 6 passed, 0 failed, 0 skipped; 0.865s |
| Existing shipment format/headers | 33 passed, 0 failed, 0 skipped; 0.114s |
| Existing economic importer | 10 passed, 0 failed, 0 skipped; 9.404s |

**Total: 94 passed, 0 failed, 0 skipped**, distinct from the older 89-pass snapshot. Three changed Python files parsed; `views/product_view.xml` parsed. Eleven registered native methods confirmed structurally only, not native PASS. The first diff check reported an extra EOF blank line in the parent's tracker append; this final evidence append ends with one newline; the subsequent `git -c core.whitespace=cr-at-eol diff --check` passed (exit0), with only Git LF-to-CRLF informational warnings. No application/source normalization or other worktree change was performed to evade a check.

### Pre-publication task boundary (historical checkpoint)

F/G are implemented with causal source-bound GREEN; H's connector contract and native tests are prepared, with the real external consumer unavailable/unverified. F-I remain unchecked because their required new-candidate native acceptance and publication have not been observed. **I is not complete.** Parent must mirror/read back the full tracker, independently verify the six-path candidate, handle any new-candidate native consent without reusing the earlier decline, create the authorized work-unit commit, update the same draft PR77 and read back final-HEAD CI. Preserve any actual global skip as SKIP, not PASS. No merge, pin, HOMO, production, live MercadoLibre/credential call, persistent cleanup, upgrade or commercial/stock/invoice write was executed by this writer.

Rollback boundary: revert only this human-review compatibility unit (shipment selection/Flex, two widget removals and corresponding tests/docs), while preserving credential-free links/private label authorization and the earlier security fixes. A deployed view rollback also requires its separately authorized database upgrade; do not restore token-bearing URLs.

### Independent compatibility verification (before publication/CI)

A fresh read-only verifier reran the same five exact cohorts against the six-path candidate: **94 passed, 0 failed, 0 skipped** (28/17/6/33/10). It found no concrete candidate blocker, new credential-pattern finding, permission expansion or sudo call. Three Python files and the XML parsed; CR-at-EOL-aware whitespace check passed. It confirmed actual default normal-user wizard and returned-URL HTTP coverage in the prepared native tests, without misrepresenting those eleven methods as executed locally. All new native acceptance, final commit/push/CI and external consumer compatibility remain pending. Native assessment reports high risk; prior candidate decline is not consent for this new candidate.

## Compatibility commit publication and native CI acceptance

Parent-verified evidence below supersedes the pending-native/publication state in earlier checkpoints without rewriting their RED/failures. This passive reconciliation performs no tests, remote/Git/RDD action or application edit.

| Boundary | Observed evidence |
| --- | --- |
| Compatibility code commit | [`ae0c2d5de0c6954664b66c6fb71457d430028a4c`](https://github.com/JoaPeralta/meli_oerp/commit/ae0c2d5de0c6954664b66c6fb71457d430028a4c) |
| Parent / exact tree | `4f95ad35dc594da236afe714be707dbae2154368` / `049bb0713d64dd1b83eb9ac8454ffebbbe3f100d` |
| Author / committer | `JoaPeralta <peralta.joaquin@hotmail.com>` for both |
| Publication | Same [draft PR77](https://github.com/JoaPeralta/meli_oerp/pull/77), no merge or pin change |
| Final code-commit CI | [Run42094445398, job127480715979](https://github.com/JoaPeralta/meli_oerp/actions/runs/42094445398/job/127480715979): **SUCCESS**, 3m36s |
| Local independent proof | Writer and independent verifier **94/0/0** (28/17/6/33/10); parent **28 passed**, 12.252s; separate executions, not additive distinct-test counts |

Actual Odoo19/PostgreSQL16 CI passed standalone cohorts **28/17/6** and showed `Starting` for **all 11 P0 native methods**, **0 P0 skips**, including the four new Flex/Full, default normal-user wizard first-print/reprint HTTP, returned-auto-URL PDF/ZPL/session/denial, and effective-product-view tests. Overall summary: **`0 failed, 0 error(s) of 460 tests`**. Exactly one global skip remains: `TestLegacyOrderNotificationResults.test_technical_actor_still_cannot_process_legacy_outcomes`, absent optional bridge actor; it is **not PASS** and certifies no optional extension. Fresh-install effective-view proof is **not a real deployed upgrade**: **MODULE UPGRADE REQUIRED** remains mandatory. Cleanup apply acceptance used disposable synthetic rows only; no persistent cleanup occurred.

F/G close within this committed-code/native-CI scope; H's bounded connector tests and dependency documentation are done but its real consumer remains explicitly **partial/unverified**. I's code verification/publication/CI is complete. Native assessment/status failed before native entry on temporary-index Access denied: `mutation_not_started`, `authority_not_evaluated`; no START/consent envelope, approval or receipt, no bypass, and no reuse of the old decline. Ordinary authorized draft publication is not native review PASS.

The parent will commit these two passive evidence documents and check that documentation HEAD's CI separately. That result is not claimed here; this checkpoint does not require an endless new self-stamping document after each passive commit. No merge, Odoo pin, HOMO/production, live credential/API action, deployed upgrade or external consumer implementation/certification is authorized by this evidence.
