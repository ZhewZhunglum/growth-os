# Code Delivery Mapping Template

Complete this document for the exact delivered commit. Do not map against a moving branch.

## Delivery identity

- Repository:
- Commit SHA:
- Tag:
- Branch:
- Delivery timestamp and timezone:
- Backend language/framework/version:
- Frontend language/framework/version:
- Database/version:
- Container/build tool:
- Developer-reported completion:
- Independently verified completion:

## Reproducibility

- Dependency lock files:
- Clean-environment setup command:
- Build command:
- Migration command:
- Seed command:
- Start command:
- Health-check command and expected response:
- Test command:
- Container image tag and digest:

## Frozen entity mapping

For each entity, record the migration/table, model, service, API/UI path, tests, and status (`PASS`, `PARTIAL`, `MOCK`, `MISSING`, `OUT_OF_SCOPE`).

| Group | Entity | Migration/table | Model/service | API/UI | Tests | Status | Gap/evidence |
|---|---|---|---|---|---|---|---|
| Identity | Principal | | | | | | |
| Identity | PermissionGrant | | | | | | |
| Product | Product | | | | | | |
| Product | ProductProfileVersion | | | | | | |
| Task | Task | | | | | | |
| Task | TaskContractVersion | | | | | | |
| Task | TaskContractPolicyLink | | | | | | |
| Task | TaskAssignment | | | | | | |
| Task | TaskStateEvent | | | | | | |
| Check | TaskCheckRun | | | | | | |
| Check | TaskCheckResult | | | | | | |
| Submission | TaskSubmission | | | | | | |
| Submission | TaskSubmissionAssetLink | | | | | | |
| Review | ReviewDecision | | | | | | |
| Asset | ContentAsset | | | | | | |
| Asset | ContentAssetVersion | | | | | | |
| Policy | PolicyDefinition | | | | | | |
| Policy | PolicyVersion | | | | | | |
| Policy | RuleEvaluationRun | | | | | | |
| Policy | RuleEvaluationResult | | | | | | |
| Channel | ChannelAccount | | | | | | |
| Channel | RuntimeEnvironment | | | | | | |
| Channel | AccountEnvironmentBinding | | | | | | |
| Channel | CapabilityState | | | | | | |
| Gate | ReleaseGateRecord | | | | | | |
| Gate | ReleaseGateEvaluationLink | | | | | | |
| Publication | Publication | | | | | | |
| Publication | PublicationEvent | | | | | | |

Framework-owned authentication session and migration-history tables are technical infrastructure, not additional business scope. They must still be documented and secured.

## Required invariant evidence

| Invariant | Database evidence | Service/API evidence | Negative test | Status |
|---|---|---|---|---|
| ProductProfileVersion is sealed and immutable | | | | |
| TaskContractVersion binds exact profile and policy versions | | | | |
| ReleaseGate binds exact Submission, ReviewDecision, AssetVersion, and Publication | | | | |
| Publication is a release intent until a valid Gate passes | | | | |
| Manual publish rechecks current Grant, Capability, Environment, and mandatory policies | | | | |
| State transitions use command id, sequence, and expected version | | | | |
| Append-only facts cannot be updated or deleted by application roles | | | | |
| Unauthorized or stale actions fail closed | | | | |
| Repeated commands do not create duplicate side effects | | | | |
| Secrets are absent from source, database plaintext, responses, and logs | | | | |

## Five-path mapping

Reference `01-V1-ACCEPTANCE-MATRIX.md`. For every path, list the exact test IDs, screenshots/logs, database evidence query, and current result.

| Path | Test IDs | UI/API evidence | Database/audit evidence | Result | Open defect |
|---|---|---|---|---|---|
| Normal publish-intent path | | | | | |
| DoR/input blocked path | | | | | |
| Review rework path | | | | | |
| Release Gate blocked/stale path | | | | | |
| Idempotency/authorization/conflict recovery path | | | | | |

## Delivery gaps

### P0

-

### P1

-

### P2 / Backlog

-

### Blueprint code that must be removed or disabled

-

## Verification outcome

- Clean build: PASS / FAIL
- Migration and seed: PASS / FAIL
- Minimum vertical loop: PASS / FAIL
- Five paths: PASS / FAIL
- Security and permission negatives: PASS / FAIL
- Staging deployment: PASS / FAIL
- Restore exercise: PASS / FAIL
- Production decision: GO / NO-GO / NOT READY

