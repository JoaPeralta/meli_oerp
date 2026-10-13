# C1 + C5 isolated local integration

## Objective
Replay the two authorized connector fixes from the exact baseline and verify their
combined behavior, preserving all original branches, worktrees and local changes.

## Authorized scope
- Baseline: bb32e4abb6088553373766a523a2c2fcf38dd4b9.
- C1: adae2962f1c885e55f576c87f323da7c64936182.
- C5: 37c81f97df4f6d031e63187acba5c930a1482080.
- Branch/worktree: codex/meli-c1-c5-integration / isolated meli-c1-c5-integration.
- Cherry-pick C1, then C5; do not integrate any other functional change.
- Branch/PR inventory for both repositories is read-only; divergent branches need approval.
- Never reapply PR 68/69 without ancestry proof; do not use remote 19.0 as baseline.
- No main/staging edits, pins, credentials, remote writes, RDD/Gentle AI, HOMO,
  Railway, database, real API, push, remote merge or deployment.
- Remote inventory uses anonymous public reads only; cached refs are not live evidence.

## Tasks and acceptance
- [x] INT-1: preserve original state, replay C1/C5, verify exact source/test blobs,
  run both focused suites and related regressions together.
- [ ] INT-2: independent review and local/cached inventory complete; live remote metadata pending;
  anonymous public access failed, so current remote branch/PR status cannot be certified.
- Inventory cannot authorize integration of the four divergent Claude branches.
- Keep all existing uncommitted files and original refs/worktrees untouched.
- Functional proof is local synthetic/source-bound; real Odoo/SDK/API proof stays pending.

## Verification
- TDD inherited ON for fixes: their earlier RED evidence is preserved; replay introduces
  no new behavior. Do not manufacture a new RED or change code merely to claim TDD.
- Runner: C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe.
- From tests/: -B -m unittest -v test_billing_info test_image_upload_security
  test_meli_parse_response test_variant_logging test_order_buyer_binding test_shipping_partner_company.
- Expected runnable count: 60 (16 Billing + 17 images + 27 shared regressions).
- Static: diff --check against baseline; AST/source scope and original blob identity.
- Independent reviewer reruns the same joined command; parent spot check before delivery.
- Never treat missing runtime imports or unavailable remote data as PASS.

## Progress and delivery
- Existing reviewed work units are replayed unchanged; no PR/delivery strategy authorized.
- Carried C1/C5 authored diff: 927 lines; no coverage reduction or artificial splitting.
- Final tracking-only commit may record integration evidence; no new functional source change.
- Evidence: ../integration-inventory-evidence/; preserve prior C1/C5 reports.
- First review boundary: exact baseline; no native review is authorized.
- C1 replay: 13dc5299ec7ae149905d251198d1dc4a58467f16; C5/integration HEAD:
  8907829276af6b3e629472a16c676f170f25e8fe. Both cherry-picks used -x, no conflicts.
- Joined foreground verification: 60 tests PASS (11.904s); shipments: 32 PASS (0.103s).
- Baseline-to-HEAD diff check and modified Python AST: PASS; all six carried blobs
  exactly match their original fix commits, including original C1/C5 ledgers.
- Original C1/C5 refs, clean indexes/worktrees and source/test hashes unchanged.
- No effective hooksPath/signing/fsmonitor; default hooks contain only .sample files.
- Odoo, requests, urllib3 and installed SDK imports unavailable; ORM, real SDK,
  wire multipart, DB and live API proof remain pending and were not attempted.
- Independent integrated review: PASS, rerun 60 + 32 tests, same-client NoSDK/SDK probe PASS.
- Parent joined spot check: 60 PASS; no functional incompatibility found within synthetic coverage.
- Connector inventory: 128 refs; receiver fields and SOLSUN already present; logo is pending,
  stock diagnostics require review, Odoo16 calendar-date improvement needs a separate port.
- Odoo inventory: 173 refs; roadmap/audit/backup-gate candidates and mixed invoice UI/test gaps.
- Cached origin/19.0 is 14 behind bb32, not live proof; PR68/69 mapping/status unavailable.
- Preserve Odoo main 18 modified + 4 untracked, roadmap 2 modified ledgers, red2 25 deletions.
- Full inventories and preservation evidence: connector-inventory/ and odoo-inventory/ under evidence.
- Remaining remote freshness gap requires permitted access; no credentials or fetch used.
- Final commit is tracking-only; verify all six inherited blobs stay identical after it.
- Rollback: isolated integration branch/worktree only; original fixes stay recoverable.
- Mirror: odd/meli-c1-c5-integration/tasks; locator odd/tasks/meli-c1-c5-integration.md.
