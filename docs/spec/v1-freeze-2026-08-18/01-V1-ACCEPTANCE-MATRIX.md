# V1 Acceptance Matrix

All paths run in isolated Staging data using the exact candidate commit, schema version, and container image. Each path uses a separate Task.

## Global evidence required for every mutating action

- Stable `command_id` and canonical `payload_hash`.
- Actual actor Principal, acting role, applicable PermissionGrant, and recorder.
- Expected aggregate version, resulting state version, event sequence, and previous event.
- Exact version foreign keys and relevant manifest/context hash.
- A rejected request must produce no partial business fact.

## Shared fixture

- One PUKO Product and one sealed ProductProfileVersion.
- Exact TaskContractVersion and TaskContractPolicyLinks.
- One Operator, one authorized Reviewer, one authorized manual Publisher, and service Principals as needed.
- Explicitly scoped and time-bounded PermissionGrants.
- One ChannelAccount, one RuntimeEnvironment, a valid AccountEnvironmentBinding, and a current CapabilityState.
- Exact active PolicyVersions and deterministic release requirements.

## AC-01 - Normal manual-publishing path

### Steps

1. Create a Task bound to exact Product, ProductProfileVersion, and TaskContractVersion.
2. Run DoR; all required TaskCheckResults pass.
3. Assign the Task and enter work in progress.
4. Create ContentAsset and immutable ContentAssetVersion v1.
5. Run DoD; all required TaskCheckResults pass.
6. Create and seal TaskSubmission #1 with v1 as its primary deliverable.
7. An authorized human Reviewer approves exact Submission #1.
8. Create Publication as `GATE_PENDING`.
9. Run the complete deterministic rule evaluation.
10. Create a PASSED ReleaseGateRecord and all ReleaseGateEvaluationLinks.
11. Recheck the current context and record `READY_FOR_MANUAL_PUBLISH`.
12. The authorized Publisher performs the external action manually.
13. Record `MANUAL_PUBLISHED_RECORDED` with external proof.
14. Complete the Task.

### Required evidence

- Unbroken TaskStateEvent sequence and no skipped transition.
- Separate complete DoR and DoD runs with expected and actual criterion counts equal.
- Sealed Submission manifest with one exact primary deliverable.
- One final APPROVED ReviewDecision for Submission #1.
- Publication existed as a release intent before Gate evaluation.
- Gate references exact Submission, ReviewDecision, AssetVersion, ContractVersion, publisher/Grant, account/environment/binding/capability, policies, and complete results.
- Publication events contain `READY_FOR_MANUAL_PUBLISH` followed by `MANUAL_PUBLISHED_RECORDED`.
- No automatic platform-publishing request occurred.

### Pass condition

The publication fact can be traced backward through exact Gate, Review, Submission, AssetVersion, ContractVersion, ProfileVersion, Task, and Product. Removing any required reference makes the test fail.

## AC-02 - DoR/input blocked and recovery

### Steps

1. Start a new DRAFT Task with one required DoR input missing.
2. Run DoR attempt 1 and record required `BLOCKED` or `FAIL` results.
3. Attempt to move the Task to READY; it must be rejected.
4. Record the Task as BLOCKED with its prior state and reason.
5. Supply the missing input without changing attempt 1.
6. Create DoR attempt 2; all required results pass.
7. Return through the recorded prior state and move to READY.

### Required evidence

- Two immutable check runs; attempt 1 retains the failed evidence.
- The rejected READY request created no TaskStateEvent.
- Event chain is `DRAFT → BLOCKED → DRAFT → READY`.
- No DoD run, Submission, Review, Gate, or Publication exists before DoR passes.

### Pass condition

Required input cannot be bypassed, and recovery requires a new complete run rather than overwriting the failure.

## AC-03 - Human-review rework

### Steps

1. Create AssetVersion v1, pass DoD run 1, and seal Submission #1.
2. Reviewer records `CHANGES_REQUESTED` for exact Submission #1.
3. Move the Task into human rework and then back to work in progress.
4. Attempts to edit v1, Submission #1, its links, or Decision #1 must fail.
5. Create AssetVersion v2, DoD run 2, and Submission #2 linked to the rework cause.
6. Reviewer approves exact Submission #2.
7. Attempt to create a Gate from Submission #1/v1; it must fail.

### Required evidence

- Two immutable asset versions, two DoD runs, two submissions, and two final decisions.
- Submission #2 identifies the superseded submission or triggering review.
- TaskStateEvent records review, human rework, resubmission, and approval without rewriting history.
- Only Submission #2, its APPROVED decision, and v2 are eligible for a Gate.

### Pass condition

Rework never mutates or silently reuses the old content, checks, submission, decision, or authorization.

## AC-04 - Release Gate blocked, stale, and re-evaluated

### Steps

1. Prepare an approved Submission and a `GATE_PENDING` Publication.
2. Revoke or expire the intended Publisher's Grant, or close the applicable CapabilityState.
3. Run the complete Gate and record a BLOCKED ReleaseGateRecord.
4. Attempts to record READY or MANUAL_PUBLISHED must fail.
5. Restore a valid, correctly scoped Grant/Capability through normal authorized action.
6. Return the Publication to `GATE_PENDING` through an event.
7. Resolve the current mandatory policy set and run a new complete evaluation.
8. Create a new PASSED Gate with a new context hash.
9. Recheck current context, then record READY and the later manual publication proof.

### Required evidence

- Both Gate records remain immutable; the blocked Gate is not overwritten.
- Each Gate has its own complete rule run and links.
- No READY or published event exists before the new PASSED Gate.
- Final publication references the new Gate.
- A change in actual Publisher, environment, binding, policy, asset, review, or capability also makes reuse fail.

### Pass condition

No projection edit or human override can bypass fail-closed behavior. Recovery always requires complete re-evaluation.

## AC-05 - Idempotency, authorization, and concurrency recovery

### A. Same command replay

- Submit a valid mutation with command C1 and payload P1.
- Repeat C1/P1.
- Expected: return the original resource/result; no second event or side effect.

### B. Same command with different payload

- Submit C1 with materially different P2.
- Expected: reject; no state or event change.

### C. Authorization rejection

- An Operator without review permission attempts to approve a Submission.
- Expected: reject before business write; an authorized Reviewer can later approve using a new command.

### D. Optimistic concurrency

- Read Task state version N.
- Submit two different commands concurrently, both expecting N and attempting the same legal transition.
- Expected: exactly one commits; the other returns conflict and creates no partial facts.

### Required evidence

- C1/P1 maps to one business fact and one event.
- C1/P2 maps to none.
- Unauthorized approval created no ReviewDecision or Task transition.
- Only one event uses the next sequence/version; the event chain does not fork.
- Failed transactions leave no orphan check, link, submission, review, gate, or publication event.
- Security/audit logs record rejection metadata without secrets.

### Pass condition

Retries are safe, permission is checked before mutation, concurrent writers cannot fork history, and clients can recover by rereading current state.

## Final sign-off

| Path | Result | Required evidence | Executor | Reviewer |
|---|---|---|---|---|
| AC-01 Normal | PASS / FAIL | Full chain IDs, queries, logs, proof | | |
| AC-02 DoR blocked | PASS / FAIL | Two runs, event chain, rejection | | |
| AC-03 Human rework | PASS / FAIL | Two versions/submissions/decisions, old-chain rejection | | |
| AC-04 Gate blocked | PASS / FAIL | Two gates, changed context, event chain | | |
| AC-05 Idempotency/auth/concurrency | PASS / FAIL | Replay, rejection, race result, event counts | | |

V1 passes only when AC-01 through AC-05 all pass, all failed requests are free of partial writes, the current projections can be rebuilt from immutable events, no unclosed P0/P1 defect remains, and the same candidate build passes final Production smoke checks.

