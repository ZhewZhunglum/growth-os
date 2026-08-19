# Decision Log - 2026-08-18 Development Meeting

Source: `C:\Users\admin\Desktop\AI 視圖：独立站破千万的視像會議 2026年8月18日.pdf`

The exported PDF visually omitted most of the page body. The decisions below were recovered from its embedded meeting-mind-map image. If the original Feishu view contains collapsed nodes or comments, those remain outside this record.

## Accepted decisions

- Deployment provider: Tencent Cloud.
- Runtime direction: Docker, PostgreSQL, and object storage.
- Production recovery objectives: RPO no greater than 1 hour; RTO no greater than 4 hours.
- V1 code rollback: use a `v1.0.0` baseline and immutable versioned build artifacts/snapshots.
- Manual publishing remains in V1.
- No plaintext password, cookie, API key, database credential, or private key may be placed in code, database plaintext fields, chat, or logs.
- Model integration does not block V1. DeepSeek is preferred and Gemini is optional, with runtime model integration planned for V2.
- V1 must cover normal execution and explicit account/environment failure, rework, retry, and blocked paths.
- Command idempotency, actual acting Principal, permission, rule version, and state events must be traceable.

## Target schedule

- 2026-08-18: developer source-code handoff begins.
- 2026-08-20: cloud-provider/service comparison and deployment recommendation completed.
- 2026-08-21: required internal milestone - first Staging deployment and minimum vertical loop.
- 2026-08-22 to 2026-08-23: internal Dogfood begins.
- 2026-08-24 to 2026-08-25: normal and negative-path testing.
- 2026-08-26: backup recovery and rollback exercise.
- 2026-08-27: Release Candidate freeze.
- 2026-08-28: final Dogfood business acceptance, not first integration.
- 2026-08-29: P0/P1 fixes and full regression only.
- 2026-08-30: conditional Production release. Failure of a mandatory gate means No-Go.

## Corrections to ambiguous meeting language

### Data retention versus disaster-recovery backup

- TK 30 days, community 45 days, and SEO 180 days are proposed default retention periods for raw business/source data, not separate physical PostgreSQL backup periods.
- A unified PostgreSQL deployment uses a common PITR and backup policy.
- Campaign or Opportunity actions create idempotent immutable manifests/version snapshots; they do not trigger full database copies.
- Core Task, Review, Policy, Publication, and Audit facts are not deleted under the raw-source retention schedule.

### Human and service keys

- Each administrator uses a separate identity, MFA, and key pair. Private keys are never shared.
- TLS keys, application encryption keys, database secrets, and model API keys are managed by a cloud Secret Manager/KMS or equivalent controlled service.
- If dual approval is required, the application records two independent approvals; it does not implement a shared-private-key scheme.

### Application rollback versus data recovery

- Application rollback changes the deployed immutable build artifact.
- Database recovery uses PITR in an isolated environment and is not an ordinary application rollback mechanism.
- Database migrations must be backward-compatible where practical or have a tested roll-forward/recovery plan.

## Pending evidence and decisions

- ~~Actual delivered language and framework.~~ Resolved 2026-08-19: Python-first Django 5.2 LTS modular monolith, Django templates with minimal browser JavaScript, PostgreSQL target runtime, Docker packaging. See `docs/adr/0001-python-first-modular-monolith.md`.
- Fixed release commit/tag and container image digest. The local repository, exact dependency pins, build instructions, migrations, and test results now exist, but the Release Candidate identity remains pending until UI and Staging gates close.
- Verification of the developer-reported 80% code completion.
- Exact definition and test mapping of the five V1 paths.
- Tencent Cloud region, services, account ownership, Staging/Production isolation, IAM/MFA, networking, database exposure, Secret Manager, logging, alerts, cost, and restore procedure.
- Exact official API model identifiers and allowed endpoints for V2 model integration.

## Progress rule

The developer-reported 80% is not a verified implementation percentage. Verified progress starts when a clean environment can reproduce build, migration, seed, startup, health check, and the frozen acceptance paths using the delivered commit.
