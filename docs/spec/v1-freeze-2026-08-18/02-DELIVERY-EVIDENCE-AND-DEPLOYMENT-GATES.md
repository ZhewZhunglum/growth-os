# Delivery Evidence and Deployment Gates

> File, proof, object-storage, and media-backup gates are superseded by [V1 Link-Only Errata — 2026-08-20](05-LINK-ONLY-ERRATA-2026-08-20.md).

This file defines the mandatory evidence and gates for the frozen minimum loop. It adds no business function.

## 1. Confirmed direction

- Cloud: Tencent Cloud.
- Application: immutable Docker image.
- Database: PostgreSQL.
- Files/proofs: object storage.
- Staging and Production: isolated.
- Production RPO: no greater than 1 hour.
- Production RTO: no greater than 4 hours.
- Production release requires a completed restore exercise.

Specific Tencent Cloud services, region, sizing, network layout, and cost remain implementation decisions that must close before Production.

## 2. Source-code delivery evidence

Every candidate release must provide:

- Repository or complete source, fixed commit SHA/tag, branch, and delivery time.
- Actual backend/frontend language, framework, runtime, and exact versions.
- Dependency lock files, repeatable build/start/test commands, and clean-environment README.
- Dockerfile/build configuration and immutable image digest.
- `.env.example` containing names and non-secret examples only.
- All migrations, checksums/order, seed/fixture, and schema output.
- Empty-database initialization and existing-version upgrade procedure.
- Application rollback or explicit roll-forward/database recovery plan.
- Automated test commands and raw results.
- Mapping from all frozen entities/invariants and AC-01 through AC-05 to code and tests.
- List of unfinished work, mocks, hard-coded shortcuts, temporary code, known defects, and disabled Blueprint placeholders.
- Secret scan and relevant dependency/security scan result.

Developer-reported percentages are not acceptance evidence.

## 3. Gate 0 - Reproducible candidate

Pass only when:

- A clean environment builds the fixed commit.
- Empty PostgreSQL migration and seed succeed.
- The app starts and health check passes.
- All P0/P1 tests pass.
- The candidate image has an immutable digest.
- Source, image, logs, and evidence contain no real secrets.
- The candidate has no unapproved Runtime expansion.

Output: release manifest, image digest, schema version, test report, mapping sheet, and known-issues list.

## 4. Gate 1 - Isolated Staging

Staging must have separate:

- Cloud identities and IAM grants.
- PostgreSQL database.
- Object-storage space.
- Secrets.
- Domain/subdomain and HTTPS.
- Logs, monitoring, and alerts.

Staging cannot read or write Production secrets or data. No shared Owner login is allowed. TLS, database/object access, minimum-permission positive tests, and access-denial tests must pass.

## 5. Gate 2 - Staging functional acceptance

- Deploy the exact Gate 0 image digest.
- Run migrations and all five acceptance paths.
- Test deployment failure, application rollback/redeploy, idempotency, authorization, concurrency, and audit evidence.
- Every code change creates a new candidate and reruns affected tests.

Pass only when AC-01 through AC-05 pass and no P0/P1 defect remains.

## 6. Gate 3 - Backup and recovery

### PostgreSQL

- Continuous WAL/archive and point-in-time recovery are enabled.
- Archive/replication delay is monitored and no greater than 15 minutes.
- Base backups are encrypted, validated, and inaccessible to the Production application writer.
- The recovery window is at least 14 days; the final value is recorded by the Owner.

A daily base backup alone does not prove a one-hour RPO.

### Objects, configuration, secrets, and audit

- Object versioning/soft-delete or equivalent recovery is enabled.
- Changed objects reach an isolated recoverable copy within 1 hour.
- Infrastructure and non-secret configuration are versioned with every change.
- Secret Manager provides recoverable secret versions without exposing secret values in evidence.
- Critical audit logs reach isolated storage within 15 minutes.

### Full restore exercise

In a new isolated environment:

1. Restore PostgreSQL and perform PITR to a chosen past point.
2. Restore required objects, configuration, and secret references.
3. Deploy the approved image.
4. Run integrity checks, authentication/authorization checks, AC-01 smoke, and audit verification.
5. Record start/end times, chosen recovery point, actual data gap, operators, checks, and results.

Pass only when the measured data gap is no greater than 1 hour and complete service recovery is no greater than 4 hours. Service recovery includes HTTPS, application, database, required objects, authentication, minimum loop, and audit; a running container alone is not recovery.

## 7. Gate 4 - Production Go/No-Go

Before Go:

- Production account/network/database/object storage/secrets are isolated.
- IAM least privilege and MFA are verified.
- Domain, HTTPS, certificate renewal, logs, alerts, and health checks work.
- Latest recoverable point is less than 1 hour old.
- PITR, object copies, and audit export are healthy.
- Deployment, rollback, monitoring, and incident owners are named.
- Production uses the same approved image digest as Staging.
- The full restore report is signed.

Any mandatory failure is `NO-GO`; the date cannot waive it.

## 8. Gate 5 - Production deployment and observation

1. Record the pre-deploy recovery point and verify backup health.
2. Run approved migration and deploy the candidate image.
3. Run HTTPS, health, authentication, database/object, audit, and minimum-loop smoke checks.
4. Stop and roll back on data corruption, unauthorized access, critical-path failure, or an expected breach of RPO/RTO.
5. Maintain enhanced monitoring for at least 24 hours and confirm the first new recovery point.

## 9. Secret and key rules

- Each administrator uses an individual identity, MFA, and individual key pair.
- No private key is shared. A need for dual control is implemented as two independent application approvals.
- TLS keys, database credentials, application encryption keys, DNS/logging credentials, and model API keys use an approved Secret Manager/KMS or equivalent controlled service.
- The database stores only secret references and non-sensitive metadata.
- Staging and Production secrets are different.
- Logs, errors, notifications, screenshots, and test reports redact secrets.

Model BYOK and provider adapters remain V2. They cannot become a V1 dependency.

## 10. Data retention and business snapshots

- TK 30 days, community 45 days, and SEO 180 days are proposed defaults for raw source/business-data retention, not separate physical PostgreSQL backup windows.
- Unified PostgreSQL uses a common PITR/backup policy.
- Campaign/Opportunity snapshots, when later implemented, are immutable version manifests and hashes, not full database copies.
- Core Task, Review, Policy, Publication, and audit facts do not use the raw-source retention schedule.
- Automated retention/deletion implementation is deferred and cannot block the minimum loop.

## 11. Target milestones

| Date | Required outcome |
|---|---|
| 2026-08-18 to 2026-08-19 | Receive source evidence; determine actual stack from code; calculate verified gaps |
| 2026-08-20 | Close Tencent Cloud architecture, service, account, security, cost, and recovery decisions |
| 2026-08-21 | First Staging deployment and minimum vertical loop |
| 2026-08-22 to 2026-08-23 | Internal Dogfood begins |
| 2026-08-24 to 2026-08-25 | Five paths and negative-path testing |
| 2026-08-26 | Backup/restore and rollback exercise |
| 2026-08-27 | Release Candidate freeze |
| 2026-08-28 | Final Dogfood acceptance |
| 2026-08-29 | P0/P1 fixes and complete regression only |
| 2026-08-30 | Conditional Production release |

## 12. Hard No-Go conditions

- Missing release manifest or mismatched image digest.
- Candidate cannot build/migrate/seed/start in a clean environment.
- Any AC path or required negative test fails.
- Any open P0/P1 defect.
- Staging shares Production database, object space, or secrets.
- Shared Owner account or missing MFA/minimum-permission enforcement.
- A real secret appears in source, image, database plaintext, logs, or evidence.
- Migration has no tested safe upgrade and rollback/roll-forward path.
- PITR/object/audit recovery is unavailable or stale beyond the target.
- Restore exercise fails RPO no greater than 1 hour or RTO no greater than 4 hours.
- HTTPS, monitoring, alerts, health checks, deployment owner, or rollback owner is missing.
- Unapproved automatic publishing, rule relaxation, or Runtime-scope expansion exists.

## 13. Release sign-off

```text
Release ID:
Git commit/tag:
Container image digest:
Schema/migration version:
AC-01..AC-05 result:
Open P0/P1 count:
Measured recovery point/data gap:
Measured full recovery time:
Known accepted risks:
Deployment owner:
Rollback owner:
Monitoring owner:

Developer lead: GO / NO-GO, name and time
Owner/acceptance reviewer: GO / NO-GO, name and time
```

