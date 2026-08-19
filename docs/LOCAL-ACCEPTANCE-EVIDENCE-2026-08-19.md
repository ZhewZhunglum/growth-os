# Local acceptance evidence — 2026-08-19

This is a development checkpoint, not Staging or Production sign-off.

## Candidate identity

- Runtime: Python / Django 5.2 LTS
- Local database: SQLite (PostgreSQL remains the target runtime)
- Test command: `powershell -File scripts/verify-local.ps1`
- Migration drift: none
- Django system checks: no issues
- Full suite: 79 tests passed

## Frozen path coverage

| Path | Automated evidence | Local result | Remaining release evidence |
|---|---|---:|---|
| AC-01 normal manual path | `core.test_acceptance.FrozenV1AcceptanceTests.test_ac01_normal_manual_publishing_path` | PASS | Repeat on exact Staging image and PostgreSQL |
| AC-02 DoR blocked/recovery | `core.test_acceptance.FrozenV1AcceptanceTests.test_ac02_dor_blocked_and_recovery` | PASS | Repeat on exact Staging image and PostgreSQL |
| AC-03 human rework | `core.test_acceptance.FrozenV1AcceptanceTests.test_ac03_human_review_rework_requires_new_exact_facts` | PASS | Repeat on exact Staging image and PostgreSQL |
| AC-04 stale Gate/re-evaluation | `core.test_acceptance.FrozenV1AcceptanceTests.test_ac04_stale_gate_is_blocked_and_requires_complete_reevaluation` | PASS | Repeat on exact Staging image and PostgreSQL |
| AC-05 authorization/idempotency/conflict | `core.test_acceptance.FrozenV1AcceptanceTests.test_ac05_idempotency_authorization_and_optimistic_conflict` | PASS | Run a true concurrent PostgreSQL lock test in Staging |

## Verified local properties

- A Task begins only as unassigned `DRAFT` at state version 0.
- DoR, explicit assignment, DoD, sealed submission, human review, Release Gate,
  and manual-publication proof cannot be skipped.
- Acting roles are checked against the persisted Principal.
- Permission DENY records win across matching product, platform, account, and
  surface scope ancestry.
- A rule result must come from an active trusted service Principal.
- Release evaluation uses the exact contract policy snapshot plus current
  mandatory policies.
- Changes to account, capability, environment, grant, policy, review, asset, or
  other release context invalidate reuse of an old Gate.
- Same command and payload is idempotent; conflicting payload or stale version
  is rejected without an event fork.

## Not yet proven

- Real PostgreSQL constraint and concurrent row-lock behavior.
- Docker image build and clean PostgreSQL boot on this machine.
- Retained real-human Dogfood evidence through the complete UI route set.
- Staging identity, HTTPS, secret injection, object storage, monitoring, backup,
  RPO <= 1 hour, and RTO <= 4 hours.

Until those items pass, the deployment decision remains **NOT READY**.
