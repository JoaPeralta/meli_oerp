# Repair incomplete Mercado Libre posting metadata

Base: `67fc3e9ea06a6a8128652bf491f8a8cefbcb532d`; branch: `fix/meli-posting-metadata`.
Objective: keep item-specific metadata complete without turning posting reads into mutations. Prepare a bounded historical repair, not a production operation.

## Authority and constraints

Local isolated connector worktree and synthetic tests only. No production or Mercado Libre traffic, credentials, cron actions, order import, invoicing, product/cost/stock or remote price mutations, push, PR, deployment, migration, or real repair. Preserve company/seller/product/variation bindings and native permissions. Existing posted prices are not a source for product price updates.

No global TDD setting was found in the target checkout/Engram; previous TDD notes concern another feature. Use observed regression-first verification for this task regardless. Standalone runner: bundled Python `-I -B -X utf8 -m unittest discover -s tests -p 'test_posting_metadata_source.py' -v`. Required native target: Odoo 19 with fresh isolated PostgreSQL, tagged connector regression and effective-view read; report separately if unavailable. Forecast exceeds 400 lines due to identity/concurrency checks and regression coverage; no remote delivery is authorized.

## Work units

- [x] **PM-01 — Initialize exact-link metadata and make posting reads pure.** Use only valid item-specific data already received; keep existing values on missing/empty/not-applicable/invalid input. Separate explicit metadata refresh from questions. Keep legitimate sibling offers distinct. Prove no HTTP/database mutations in reads, refresh without questions, false-success behavior, and malformed/HTTP/auth handling.
- [x] **PM-02 — Prepare bounded historical repair and runbook.** Explicit rows; verify seller, exact item/variation and independent product binding; default preview; protect against concurrent change; no remote mutation, product write, automated cron or implicit commit. Apply requires separate authority and reviewed plan. Prove preview/apply scope, idempotence, and stale/conflicting evidence rejection. Document deployment and module upgrade, rollback, 42-row recovery, evidence limits and separate commercial risks.

## Evidence and next step

The prior production list showed 42/124 rows missing status, price and permalink; evidence does not reconstruct the history of each row. The user supplied the deployed build attestation; this work has no verified read-only database or historical logs credential and will not probe one. Prepare code and tests locally.

## Verified outcome

- Both work units close in the same coherent local commit `765a39f0868433c44b46d73f8c97b8b28e08cda5` (tree `8eff1d720722d7db3d0e10f8a76a10e9b15e763a`). Shared model and authorization boundaries needed to land together after native verification; no PR slice or remote delivery.
- Baseline: 2 expected failures in new source-bound regressions; 17 existing native tests passed on the exact original tree.
- Final: 6 focused offline tests and 32 standalone stock-route regressions passed. Odoo 19.0-20260723 with fresh loopback PostgreSQL 18.4 and a blocked-egress negative control: 39 native tests, zero failures/errors, 11 installed connector crons and 0 active.
- Separate synthetic upgrade: installed exact base then ran `-u meli_oerp` against the same fresh synthetic database. 5 native smoke checks passed; stored wizard view kept its record identity, new button/manager restriction and bound action were observed; 11 crons, 0 active. Both clusters stopped and temporary secrets removed.
- Exact tested index manifest SHA256: `e6f54316d400a2c82349540446809cb20457c827dad43c0e9de8b518b3c2f18a`; final source matches committed blobs. Native runner `../metadata-native-lab/run-native.ps1`; evidence folders `corrected-native` and `corrected-upgrade`.
- Intermediate failures preserved: PM01 synthetic manager omitted the base internal group (1/24 failing); corrected fixture, no privilege elevation. The first 39-case full candidate run exposed six repair errors from local `_` shadowing Odoo translation; fixed and rerun. Native checks do not certify entire CI or production database.
- Independent source verification reviewed roles, scope, locking and no remote-write path. PostgreSQL selected row locks cannot stop new duplicate-key inserts: real repair requires a serialized maintenance window.
- Rollback boundary: revert this local feature commit and, after any separately authorized rollout, restore matching module view records and verify effective view. Historical fills, if ever approved, require separate per-row reversible evidence.
- No production access on this turn, no current 42-row IDs/logs, no actual repair, no publication, no deployment. Next: normal code review, then separately authorized rollout and controlled per-MLA preview/apply only for provable bindings.
