# Dogfood V1 Runtime Freeze

> File delivery and storage requirements are superseded by [V1 Link-Only Errata — 2026-08-20](05-LINK-ONLY-ERRATA-2026-08-20.md).

| Item | Frozen value |
|---|---|
| Status | `FROZEN` |
| Freeze date | 2026-08-18 |
| Business scope | Single company, single B2C physical-product pilot: PUKO |
| Primary market/language | US / English |
| Roles | Owner, Operations Admin, Operator |
| Reviewer/Publisher | Granted by scoped PermissionGrant, not additional fixed roles |
| Publishing | Manual only |
| Cloud direction | Tencent Cloud |
| Runtime direction | Docker, PostgreSQL, object storage |
| Production recovery | RPO no greater than 1 hour; RTO no greater than 4 hours |
| Target final Dogfood | 2026-08-28 |
| Conditional release window | 2026-08-29 to 2026-08-30 |

This file is the authoritative V1 Runtime boundary. Earlier field contracts provide implementation detail only where they do not conflict with this freeze or an accepted Errata.

The freeze permits technical tables owned by the selected authentication framework, migration tool, job runtime, and observability stack. Those tables are infrastructure, not new business scope, and must be documented and secured.

## 1. Required outcome

V1 proves one auditable manual-publishing workflow:

```text
Login/RBAC
→ sealed Product Profile
→ Task + exact Contract
→ DoR
→ work + immutable ContentAssetVersion
→ DoD + sealed Submission
→ human Review
→ fail-closed Release Gate
→ manual publish record and proof
→ complete audit/transition chain
```

External content performance is not a V1 pass/fail criterion. V1 is accepted on workflow correctness, authorization, immutability, idempotency, auditability, failure handling, deployability, and recoverability.

## 2. Runtime business-entity whitelist

Only the following 28 business entities belong to the first-priority minimum vertical loop.

### Identity and authorization

- `Principal`
- `PermissionGrant`

### Product context

- `Product`
- `ProductProfileVersion`

### Task and contract

- `Task`
- `TaskContractVersion`
- `TaskContractPolicyLink`
- `TaskAssignment`
- `TaskStateEvent`

### DoR and DoD checks

- `TaskCheckRun`
- `TaskCheckResult`

### Submission and review

- `TaskSubmission`
- `TaskSubmissionAssetLink`
- `ReviewDecision`

### Content assets

- `ContentAsset`
- `ContentAssetVersion`

### Policy and rule evaluation

- `PolicyDefinition`
- `PolicyVersion`
- `RuleEvaluationRun`
- `RuleEvaluationResult`

### Account, environment, and capability

- `ChannelAccount`
- `RuntimeEnvironment`
- `AccountEnvironmentBinding`
- `CapabilityState`

### Release Gate

- `ReleaseGateRecord`
- `ReleaseGateEvaluationLink`

### Manual publication record

- `Publication`
- `PublicationEvent`

No later-priority entity may become a dependency of this loop.

## 3. Required acceptance paths

1. Normal end-to-end manual-publishing path.
2. DoR/input blocked path and recovery through a new check run.
3. Human-review rework path through a new asset version and submission.
4. Release Gate blocked/stale path and complete re-evaluation.
5. Idempotency, authorization rejection, and optimistic-concurrency recovery.

Detailed steps and evidence are defined in `01-V1-ACCEPTANCE-MATRIX.md`.

## 4. Frozen invariants

### Identity, authorization, and secrets

- Every human and service action records the actual Principal, acting role, applicable PermissionGrant, and recorder.
- Assignment never grants review or publish permission.
- Staging and Production use separate identities, secrets, databases, and object-storage spaces.
- No real password, cookie, API key, private key, or connection string appears in source, seed data, chat, logs, evidence packs, or `.env.example`.
- Human administrators use separate identities, MFA, and separate key pairs. Private keys are never shared.

### Sealed versions

- `ProductProfileVersion`, `TaskContractVersion`, `ContentAssetVersion`, completed checks, submissions, review decisions, gate records, rule results, and publication events are immutable facts.
- A sealed profile records its manifest/hash and sealing actor/time. It cannot later acquire or lose content under the same version ID.
- `TaskContractVersion` binds the exact `ProductProfileVersion` and uses `TaskContractPolicyLink` for exact `PolicyVersion` foreign keys.
- New asset content always creates a new `ContentAssetVersion`.
- Rework always creates a new check run and a new `TaskSubmission`; it never edits the old submission or review.

### Commands, events, and concurrency

- Every mutating command has stable `command_id`, canonical `payload_hash`, and expected aggregate version.
- Same command plus same payload returns the original result without another side effect.
- Same command plus different payload is rejected.
- Event order uses per-aggregate `event_sequence`, `previous_event_id`, and optimistic version checks, never timestamps.
- Failed or unauthorized commands create no partial business facts or orphan links.

### DoR, DoD, and human review

- DoR, DoD, Release Gate requirements, and Success Criteria remain separate concepts.
- A required `FAIL`, `BLOCKED`, `ERROR`, or `SKIPPED` result cannot aggregate to PASS.
- READY requires a complete passing DoR run.
- Submission requires a complete passing DoD run and at least one exact primary deliverable.
- Every submission has at most one final review decision in V1.
- Human content approval is necessary but never sufficient for publication authorization.

### Release Gate and publication

- V1 mandatory PolicyVersions are introduced through reviewed seed/migration data. V1 exposes no general policy editor, runtime activation workflow, or automatic rule relaxation.
- `Publication` begins as a release intent in `GATE_PENDING`; its existence does not authorize publishing.
- `ReleaseGateRecord` binds the exact Publication, TaskSubmission, APPROVED ReviewDecision, primary ContentAssetVersion, TaskContractVersion, authorized publisher and Grant, ChannelAccount, RuntimeEnvironment, AccountEnvironmentBinding, CapabilityState, policy set, and complete RuleEvaluationRun/Results.
- Any missing, expired, unknown, failed, blocked, or inconsistent required input causes fail-closed.
- A change to publisher, Grant, Capability, environment, account binding, mandatory policy, asset version, submission, review, or gate validity invalidates reuse of the old Gate.
- Before manual publishing, the current context is checked again. Only then may `READY_FOR_MANUAL_PUBLISH` be recorded.
- The external platform action is performed by an authorized human outside the V1 system.
- The system records `MANUAL_PUBLISHED_RECORDED` with exact Gate, publisher, recorder, Grant, timestamp, external ID/URL, and proof reference/hash.
- V1 contains no automatic platform-publishing call.

## 5. Explicitly deferred from the minimum loop

The following remain later migrations and cannot block the first loop:

- External evidence, Topic, Demand Assessment, Product Opportunity, Initiative, and Channel Plan.
- Manual/CSV Performance and SEO observations.
- Thin GEO measurements.
- Human Learning.
- Issue reporting.
- Commerce and process telemetry.

They are ordered in `03-POST-CLOSURE-BACKLOG.md`.

## 6. Blueprint only

- Full meeting governance and automated Replay/Shadow/Canary/Activation.
- Automatic rule approval, activation, relaxation, or safety rollback.
- Automatic publishing, advertising, budget changes, account creation, or community creation.
- Complex attribution, causal scoring, dependency graphs, multiple primary assignees, and full asset graphs.
- All-platform connector rollout, automatic multi-model routing, local-model deployment, B2B, CRM, multitenancy, and billing.

Blueprint items do not receive V1 runtime tables, APIs, pages, background jobs, or integrations.

## 7. Definition of Done

A V1 item is complete only when all apply:

- It exists in the fixed delivered commit and has an executable migration.
- A clean environment can migrate, seed, start, and pass health checks.
- Normal and required negative paths have repeatable tests.
- Authorization, idempotency, immutability, concurrency, and audit assertions pass.
- It runs in Staging from the exact immutable build intended for release.
- It does not depend on deferred or Blueprint functions.
- Mocks, hard-coded shortcuts, limitations, and defects are disclosed.

A screenshot, oral demonstration, completion percentage, or success on an existing developer database is not sufficient evidence.

## 8. Change discipline

New ideas go to the backlog by default. V1 changes only when a documented P0 issue would otherwise cause unauthorized access/publication, broken audit, unrecoverable data corruption, Release Gate bypass, failure of a required acceptance path, or failure of the locked RPO/RTO.

Every accepted P0 change records its reason, affected entities and paths, compatibility and migration impact, new tests, rollback/recovery plan, and Owner approval.

The 2026-08-30 date never overrides the gates. Any unmet mandatory condition results in `NO-GO`.
