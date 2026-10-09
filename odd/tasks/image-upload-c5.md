# C5 image upload credential safety

## Objective and rationale
Remove access tokens from image upload URLs/query parameters while preserving
multipart files and all active NoSDK/SDK consumer contracts.
The defect is token placement; no real token leak has been demonstrated.

## Authorized scope
- Base: bb32e4abb6088553373766a523a2c2fcf38dd4b9; branch codex/image-upload-c5.
- Isolated worktree only; preserve C1 adae2962f1c885e55f576c87f323da7c64936182.
- Active NoSDK/SDK upload/uploadfiles and focused tests; minimal upload-local diagnostics.
- No changes to OAuth, retries, other HTTP methods, fiscal policy, C3/C4/C6/C7,
  other repositories/modules, credentials, real APIs, DBs, HOMO/Railway, deployed pins,
  crons, ARCA, push, merge or deployment; no new dependencies.
- Local C5 commit only AFTER satisfactory tests and independent review.
- No C1 integration. Native review is not preauthorized; report consent if required.

## Task and acceptance
- [ ] C5: reproduce the unsafe effective request; fix centrally; verify both backends,
  all image consumers and related regressions; obtain independent review; commit locally.
- Prefer upload() delegation to uploadfiles() without changing consumers.
- Preserve multipart field file, filename, bytes, MIME, timeout/retry policies,
  response.json(), self.response, self.rjson and each backend's return contract.
- Token belongs only in Authorization: Bearer, never outgoing URL/query or diagnostics.
- Cover JPEG success/id/variations, 400/401/403/429/5xx, malformed/incomplete payload,
  timeout/transport errors and diagnostic redaction, including short synthetic tokens.
- Inventory: three direct product.py consumers and multi-image/publication/variant wrappers.
- Inactive vendored melisdk/meli.py is not the active SDK; disclose residual scope honestly.

## Verification
- TDD: ON, explicit user RED-first requirement; observed RED before production writes.
- Runner: C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe.
- Focused: -B -m unittest discover -s tests -p test_image_upload_security.py -v.
- Related: same runner/discover for test_meli_parse_response.py, test_variant_logging.py,
  test_order_buyer_binding.py and test_shipping_partner_company.py.
- Static: git diff --check; AST parse modified Python; inspect exact patch/scope.
- Block sockets; all HTTP synthetic. Odoo/requests/urllib3/SDK/pytest/Postgres unavailable.
- Real ORM, installed SDK and wire multipart/API integration are pending, not PASS.
- Parent spot check after writer; separate independent reviewer before local commit.

## Delivery and progress
- Strategy: exception-ok; user-approved C5-only size:exception on 2026-10-09; no PR/integration.
- Justification: cohesive C5 fix, mostly tests; independently found reflected-token leak fixed.
- All focused/regression checks passed; do not remove coverage, security controls or split artificially.
- First review boundary: bb32e4abb6088553373766a523a2c2fcf38dd4b9.
- RED observed before production edit: 12 methods, 35 assertion/subtest failures,
  zero setup errors, exit 1 (writer/red-clean-harness.txt). Earlier harness attempt
  had 36 failures/2 fixture errors; corrected fixtures before touching production.
- Baseline meli_util.py SHA256: CCEC361A38F8854A9754FE5BEA24D1637F8D275884D4EB7E6C4A658B73D3BA42.
- Initial GREEN: focused 15 PASS; related parser 8, variant logging 5, buyer binding 9,
  shipping partner company 5 PASS (42 total); git diff --check and AST scope PASS.
- Reused existing uploadfiles in both backends; upload-only exception redaction
  handles raw/quoted/quote_plus and short tokens. Public signatures/state preserved.
- Existing image download, posting error and HTTP backend suites were attempted:
  exit 1 import failures (missing Odoo/requests); unavailable, not behavioral PASS.
- Exact AST contracts cover all three direct consumers and variant/template/banner
  multi-image traversals, without real ORM writes or wire multipart encoding.
- Initial independent review: BLOCKED; reflected HTTP 401 credentials reached
  client.json() and actual consumer logs (review/report.md, reflected-http-error.txt).
- Scoped correction RED: 17 methods, 9 assertion/subtest failures, zero errors;
  correction-red-consumer.txt executes actual consumer logger before assertions.
- Correction GREEN: focused 17 + related 27 = 44 PASS; AST scope/diff check PASS.
- Upload-local recursive JSON/text redaction preserves shapes/status/non-sensitive
  data and leaves the original HTTP object/body untouched for SDK compatibility.
- Independent recheck: PASS, 44 tests; original blocker resolved (review/recheck-report.md).
- Parent focused spot check: 17 PASS after correction and EOF normalization; staged diff check PASS.
- Local commit: authorized after exact-candidate verification; no hooks/validators bypassed.
- RDD/Gentle AI: no operations authorized by this approval; not executed for C5.
- Evidence outside checkout: ../c5-evidence/; no real secrets.
- Rollback boundary: C5 upload methods/helper, focused tests and this ledger only.
- Mirror: odd/image-upload-c5/tasks; locator odd/tasks/image-upload-c5.md.
- Exception procedure: user-provided AGENTS Delivery Strategy/ODD and work-unit-commits skill, lines 73–98.
- Observed authored size: 540 = 77 production + 389 tests + 74 ledger lines.
  Approval is limited to this candidate and one local commit, not future changes or remote delivery.
