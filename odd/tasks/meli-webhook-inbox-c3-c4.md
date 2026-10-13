# MercadoLibre webhook inbox C3/C4 — initial local implementation

## Objective and boundaries

Implement approved native intake, retry metadata and inactive bridge worker only. Commercial readiness is conditional on real Odoo/PostgreSQL durability, idempotency and permission proof. Do not modify the commercial importer; investigate coordination through an isolated prototype.

- Connector branch: `codex/webhook-inbox-c3-c4`, base `cb5f69c4cf3d91a3c29be2a5f5f52a2d44a0ff3f`.
- Bridge branch: `codex/c3-c4-inbox`, base `798a3ad3bc3b03788395e85aa8ac87127e69ed5d`.
- Preserve C1/C5 and all original worktrees/dirty files.
- No HOMO/Railway/pins/main/staging, commercial credentials/API/OAuth/refresh, ARCA, cron activation, push/merge/deploy or Gentle AI/RDD.
- Local disposable databases/servers use synthetic data, explicit private paths and loopback only; no backups or existing databases.
- Functional imports, automatic invoicing and unapproved external effects remain blocked, including legacy dispatch of new inbox receipts.

## Workflow and delivery

Current feature strict-TDD configuration: UNKNOWN; prior feature-specific ON/OFF choices are not inherited. Use explicitly observed test-first RED -> GREEN for this feature without changing global mode. Runner: `C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe -I -B -m unittest discover` with focused suite patterns; real Odoo/PG commands must be recorded once established.

Only ordinary independent review is authorized. No native assessment/status/review or fabricated native PASS. Commit each coherent unit locally only after its functional tests and independent review pass; no AI attribution.

Delivery strategy: `ask-on-risk` for any future PR. No PR is being prepared or authorized now. Forecast: connector intake 600–850 authored lines; paired bridge binding 140–230; retry metadata 150–240; worker 400–650. The 400-line heuristic does not justify deleting coverage or artificial splitting; future PR sizing/exception remains pending. No active local commit hook or local size cap was found.

## Implementation checklist

- [x] T1 — Original HTTP POST routes, bounded canonical intake, safe duplicate/conflict handling, UUID missing-ID receipts, explicit fail-closed bridge identity binding and pre-client commercial barrier for new receipts. Focused tests, corrected independent source review and real disposable HTTP/PG checks passed; paired local commit identities follow after creation.
- [ ] T2 — Only `local_attempts` and indexed `next_attempt_at`, four states and historical metadata unchanged, versioned non-requeue upgrade preparation and tests.
- [ ] T3 — Bridge-owned inactive cron/private non-sudo A entrypoint, minimum notification permissions/company boundary, due-row `SKIP LOCKED` selection and rollback-only claim demonstration. Unconditional commercial not-ready barrier, no GET/import/commit progress.

## Acceptance and verification

1. Intake validates body/identity/company/topic/resource before any capable client, remote call or business dispatch; no manual controller commit.
2. Namespaced and matching legacy IDs compare complete canonical binding; mismatches are quarantined; invisible duplicate winners and unrelated DB failures never become ACK success.
3. New inbox rows cannot be consumed by legacy UI/RPC/batch processors. Historical state/attempt/timestamp values are not automatically rewritten or requeued.
4. Technical identity is XMLID-resolved with explicit company, su=False and A; new cron stays inactive and eleven connector crons unchanged. Claim writes/locks roll back, leaving no commercial effects or recorded attempts.
5. Run focal suites, existing C1/C5 60 checks and shipment32, resource protections, bridge identity/Full-stock checks, diff/AST/XML/CSV checks. Record failures/unavailable checks honestly.
6. Disposable proof: actual raw HTTP/status/commit-before-ACK/latency/duplicate/commit fault, PG RR snapshot/40001/23505/child/variation/pack races and actor ACL/rules where feasible. SQL-only prototypes do not prove actual stock/accounting effects.
7. Shared commercial coordination requires concrete fresh-snapshot proof across webhook/poll/resync/manual; no importer architecture expansion without a new decision. Never claim C3/C4 complete or ready for real orders.

## Progress and recovery evidence

Preparation verified: gh-only transient helper fetched exact main SHA with no refs/FETCH_HEAD/worktree/config changes; complete bridge tree equals local81d; new worktrees start clean. Main's 18 modified/four untracked entries remain preserved. Initial survey found native PG18.4; a pinned synthetic Odoo19 runtime was subsequently prepared only in the owned lab.

- T1: corrected source independent PASS; focus35/binding5/pinned facade6/edge9 and C1+C5 92 pass. Real Odoo/PG HTTP22 and ORM replay8 pass on exact reviewed bytes; native commit precedes ACK and native23503 yields500/no durable loser. Original source FAIL and facade HTTP500 preserved. Ordinary sampled maximum445.265ms, injected800ms commit-delay cases1029.133/926.962ms exceed500ms; no worst-case SLA claim. Unknown/chunked framing411 needs later provider TEST compatibility proof. Paired commits pending creation.
- T2: pending. No migration executed.
- T3: pending. No cron activation authorized.
- Disposable lab/prototype: owned PG18.4 loopback cluster and pinned Odoo19 synthetic lab prepared; SQL coordination experiments 7/7 pass, installed notification/parent indexes verified. HTTP/ACL/worker/business-effect proof pending corrected candidate. Same-value SQL tuple touch works; cached ORM empty writes are not guaranteed physical touches. Evidence directory: `C:/Users/peral/.codex/visualizations/2026/10/09/01a120e6-d01c-7f60-b6e3-3964776a16d2/c3-c4-implementation`.

## Rollback and remaining approvals

Each code unit removes only its new intake/metadata/private-worker behavior; preserve receipt/history data and never perform destructive downgrade. Keep worker inactive during rollback. Public API/security/runtime proof remains separate from source declarations. Unit4 commercial import coordination and units5–6 retries/operational acceptance remain incomplete until their gates pass.

Next: test-first T1 in the new worktrees, safe disposable prototype in isolated evidence; independent review before local commits.
