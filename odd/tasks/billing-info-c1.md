# Billing Info C1 HTTP response correction

## Objective
Consume valid new Billing Info responses without an unnecessary legacy request,
using per-response status and payload snapshots for mutable NoSDK/SDK clients.

## Authorized scope
- Base: bb32e4abb6088553373766a523a2c2fcf38dd4b9.
- Branch: codex/billing-info-c1; isolated worktree only.
- Change get_billing_info() and its focused tests/registration only.
- Preserve fiscal normalization/policy, fallback triggers/order, and global GET behavior.
- No OAuth, webhooks, stock, publication, other modules, credentials, network requests,
  database changes, Railway, deployment, deployed pins, push, or merge.
- Keep original checkouts and their existing work untouched.

## Problem and rationale
The caller reads status_code while both connector backends return a mutable client
with last_status_code. A valid new HTTP 200 is discarded and legacy failure loses
available billing data. Known error responses must not be accepted as billing success.

## Implementation and checks
- [ ] C1: reproduce RED; implement minimal response snapshots/validation;
  run focused/regression checks; obtain independent review; commit the verified unit.
- New endpoint 200: valid nested/root payload, one GET, existing normalizer unchanged.
- Preserve legacy selection; reject known non-200/error/malformed responses.
- Legacy SDK missing status stays supported without claiming HTTP confirmation.
- Cover aliases, incomplete envelopes, timeouts, absent billing ID and missing identity.
- TDD: ON, explicit user instruction; RED before fix, then GREEN and review/refactor.
- Runner: C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe.
- Focused: -B -m unittest discover -s tests -p test_billing_info.py -v.
- Regressions: same runner/discover for test_meli_parse_response.py,
  test_order_buyer_binding.py, test_shipping_partner_company.py, test_variant_logging.py.
- Static: git diff --check; AST parse changed Python files.
- No real Odoo/DB/SDK/Requests runtime proof is available; all HTTP fixtures synthetic.

## Delivery and progress
- Delivery strategy: ask-on-risk; original forecast <=370; observed authored count 386 including ledger.
- One work-unit commit; no PR/push/merge/deployment authorized.
- RDD: ON, global decision; parent owns assessment/consent and exact native continuation.
- First review boundary: bb32e4abb6088553373766a523a2c2fcf38dd4b9.
- RED: original orders.py SHA256 7B34190EEEB57BE2D8AABD7AB7B67C79D5A3BA466442E084FD0E1005FD7FE7F1;
  focused 14 methods, 14 failed assertions/subtests and 4 missing-field errors, exit 1.
  The four errors are KeyError DOC_NUMBER after the defective caller returned order ID only,
  not setup failures. Full output: ../c1-evidence/writer/red.txt (outside this worktree).
- GREEN: focused 16 PASS; parse-response 8 PASS; buyer binding 9 PASS;
  shipping company 5 PASS; variant logging 5 PASS. Static diff/AST checks PASS.
  Exact commands are above; full logs are outside this worktree in ../c1-evidence/writer/.
- Compatibility: legacy SDK unknown status accepts fresh valid payload without claiming 200.
  Same-object payload reuse is conservatively rejected to prevent swallowed-error stale replay.
- Registration: standalone source-bound suite follows existing pure tests; tests/__init__.py unchanged.
- Final-suite pinned AST replay (after patch, not the first RED): 16 methods,
  17 failed assertions/subtests and 3 missing-field errors; NoSDK one-GET fails as 2 != 1.
  Final focused recheck: 16 PASS; baseline-replay.txt/final-green.txt retain full output.
- Independent review: PASS, no blocking findings; independently reran 43 tests (0 FAIL).
- Commit and assessed tier/outcome: pending.
- Rollback boundary: this ledger, focused test/registration and get_billing_info changes only.
- Mirror: odd/billing-info-c1/tasks; repository-relative locator odd/tasks/billing-info-c1.md.
- Parent spot check: focused suite 16 PASS, exit 0; independent report ../c1-evidence/review/report.md.
- Next: local work-unit commit, native assessment/consent; no remote delivery.
