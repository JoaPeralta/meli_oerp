# LOCAL PR DRAFT — share historical order economics

Implement one pure interpreter, complete pre-write MP/discount acquisition and a
minimal validated audit. Remove silent seller estimation and coupon proration.
Review pure rules/tests first, then the traditional projection and bridge contract.

## Exact source candidate and dependencies

- Branch: `codex/meli-economic-unification`.
- Connector reference/base: `0232e3a0104326dc01f9fd9e4df925cf8978a11b`.
- Source candidate: `38c9909a5c5901e6cd53eba8e81e61bced50d9f7`.
- Work units: `a80869e1729c83bc2329fa52e6fc8504ca4688c7`,
  `8f268d47462556442d8dfd037ab593ada595e251`,
  `dd09c50f4415fcdda3f3b105a70c44a9ae77ee03`,
  `38c9909a5c5901e6cd53eba8e81e61bced50d9f7`.
- This Markdown-only delivery follow-up does not change that source candidate.
- Odoo companion starts at `5584130895f934e90dd9c888ca2af68af00e136a`;
  its source integration is `f901e5a0f734e986e890d39d5c9cf8efc6309000`
  plus six-reason correction `d2c87779f9210661ad4d185252e92deb59f873be`.
  Integrate this connector
  before the companion's final approved pin. Do not merge unrelated branches.

## Changes

| Area | Change |
| --- | --- |
| models/meli_order_economics.py | Pure exact Decimal evidence, attribution and typed pending reasons. |
| models/orders.py | Pre-write complete acquisition, reuse and idempotent Json snapshot; retain native tax application. |
| tests and docs/order-economics.md | Synthetic source regression matrix, native negatives prepared, documented evidence limits. |

## Verification and supported scope

Observed source matrix: **282 methods PASS in 14 suites** (connector154, Odoo128).
Runner: `C:/Users/peral/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe -I -B -X utf8 -m unittest discover -s tests -p <pattern> -v`.
The connector regression batch used the same runner without `-v`; each process exited0.
Odoo deployment-impact declaration checked against its full reference diff: PASS.
Cross-repository variables bind only the two new worktrees; canonical Odoo is used
read-only for Git identity validation. AST8 files and both diffs checked clean.
Three focused review reproductions were RED before the correction, then GREEN.

| Evidence/modality | Result and limit |
| --- | --- |
| Consistent200, explicit item/quantity/price/funding, matched MP population | Validated by the single pure interpreter; existing operational admission still applies. |
| Seller/marketplace/shared funding; coupon; cashback | Explicit allocations audited; effective price not discounted twice; cashback does not alter invoice basis. |
| Missing/invalid/duplicate payment IDs; failed/partial MP acquisition | Fail closed, no valid prefix accepted or order-only fallback. |
| Nested/unbounded audited metadata; conflicting coupon/campaign IDs | Precise pending/contradictory reason, no audit replacement. |
| Exact404 `discount_not_found` | Pending `discount_not_found_evidence_incomplete`; successful404 NOT IMPLEMENTED without independently sufficient evidence. |
| Unknown funding/allocation, financing/refunds, external rounded-input mismatch | Pending/unsupported, no estimates or invented tolerance. |
| Multi-item/pack/logistics execution; non-full coupon invoice policy | Existing operational blockers preserved; no expanded homologation claimed. |

Native tags `meli_economic_unification` (bridge) and
`meli_economic_evidence_negative` (connector3 new negatives) are prepared,
**NOT RUN**: local Odoo/PostgreSQL runtime unavailable. Native ORM/ACL/savepoint,
tax/currency projection and full commercial graph rollback remain required proof.
No HOMO or production certification, real import, cron activation or fiscal action.

## Integration and Railway homologation (NOT EXECUTED)

1. Obtain native synthetic ORM/ACL/rollback proof with the exact repository pair;
   resolve the successful404 evidence decision separately, never silently weaken it.
2. Review and integrate connector first; record its approved immutable SHA.
3. Only after required proof, update Odoo `MELI_OERP_SHA` to that exact SHA and
   rerun both repository contract suites and deployment-impact validation.
   **The Dockerfile pin is NOT modified by this draft.**
4. With a separately authorized Railway HOMO session and backup/rollback plan,
   build the exact pair, keep crons/imports disabled, and perform a controlled
   `-u meli_oerp` under the repository's maintenance procedure. Code deployment
   alone does not create `economic_snapshot`.
5. Verify module state, the actual JSON column/registry, effective actor ACLs,
   unchanged tax/warehouse/fiscal controls, synthetic reimport idempotency and
   negative rollback before any separately authorized real-order homologation.
   No production promotion until those results are approved.

Rollback: revert only these feature commits/pin; preserve unrelated pending work.
If a schema upgrade was performed later, use the approved backup/rollback plan;
this draft neither drops data nor backfills existing snapshots.

## Publication prerequisites

LOCAL DRAFT ONLY: no PR URL, push or GitHub mutation exists. An approved issue,
actual repository target branch/labels and an explicitly authorized destination,
operation and credential/session must be resolved before remote publication.
No approved issue/CI result is invented; the generic skill's Gentle-AI-specific
CI/branch rules are not asserted as this repository's actual hooks.

## ODOO DEPLOYMENT IMPACT

**Modules:** meli_oerp
**Upgrade required:** YES
**Reason:** New `mercadolibre.orders.economic_snapshot` Json column/registry field.
**Database, config or data change:** New nullable audit field; no backfill or cron change.
**Backup or rollback needed:** Backup before a later authorized upgrade; preserve existing data.
**Post-deploy:** Controlled module upgrade and actual registry/column/ACL verification,
not a deployment-only claim. No new UI/XML is introduced here.

<!-- odoo-upgrade: required -->
<!-- odoo-upgrade-modules: meli_oerp -->
<!-- odoo-upgrade-reason: New economic_snapshot Json field requires database and registry update. -->
