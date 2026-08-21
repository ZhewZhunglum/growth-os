from __future__ import annotations

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAsset, ContentAssetVersion, ReviewDecision, TaskSubmission
from products.models import Product, ProductProfileVersion
from workflow.models import (
    Task,
    TaskAssignment,
    TaskCheckRun,
    TaskContractPolicyLink,
    TaskContractVersion,
)

from releasegate.models import (
    AccountEnvironmentBinding,
    ActingRole,
    CapabilityState,
    ChannelAccount,
    PolicyDefinition,
    PolicyVersion,
    Publication,
    PublicationEvent,
    ReleaseGateRecord,
    RuleEvaluationResult,
    RuleEvaluationRun,
    RuntimeEnvironment,
    canonical_sha256,
    release_context_hash,
)
from releasegate.services import (
    orchestrate_v1_release_gate,
    record_manual_publication_proof,
)


def digest(label: str) -> str:
    return canonical_sha256({"label": label, "nonce": str(uuid.uuid4())})


class ReleaseGateDomainTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.owner = Principal.objects.create_user(username="owner", role=Principal.Role.OWNER)
        self.reviewer = Principal.objects.create_user(
            username="reviewer", role=Principal.Role.OPERATIONS_ADMIN
        )
        self.publisher = Principal.objects.create_user(username="publisher")
        self.rule_evaluator = Principal.objects.create_user(
            username="rule-evaluator",
            principal_type=Principal.PrincipalType.SERVICE_ACCOUNT,
        )
        self.product = Product.objects.create(
            product_code="PUKO", name="PUKO", market_code="US", language_code="en",
            created_by_principal=self.owner, updated_by_principal=self.owner,
        )
        self.profile = ProductProfileVersion.objects.create(
            product=self.product, version_number=1, market_code="US", language_code="en",
            audience={"primary": "US consumers"}, core_value_proposition="Daily wellness",
            brand_voice={"tone": "measured"}, product_facts={"mode": "B2C"},
            prohibited_expressions=["cure"], created_by_principal=self.owner,
        )
        self.profile.seal(self.owner)
        self.edit_grant = self._grant(self.owner, "EDIT")
        self.assign_grant = self._grant(self.owner, "ASSIGN_TASK")
        self.review_grant = self._grant(self.reviewer, "APPROVE")
        self.publish_grant = self._grant(self.publisher, "PUBLISH")
        self.rule_evaluation_grant = self._grant(self.rule_evaluator, "REVIEW")

        self.policy_definition = PolicyDefinition.objects.create(
            policy_code="V1_RELEASE_INTEGRITY", name="V1 release integrity",
            created_by_principal=self.owner, updated_by_principal=self.owner,
        )
        self.policy_version = PolicyVersion.objects.create(
            policy_definition=self.policy_definition, version_number=1,
            rules=[{"rule_code": "exact_release_context", "required": True}],
            effective_from=now - timedelta(minutes=5), created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.contract = TaskContractVersion.objects.create(
            product_profile_version=self.profile, version_number=1, title="TikTok manual publication",
            dor_criteria=[{"key": "input", "required": True}],
            dod_criteria=[{"key": "deliverable", "required": True}],
            release_gate_criteria=[{"key": "exact_release_context", "required": True}],
            success_criteria=[{"key": "proof_recorded", "required": True}],
            sealed_at=now, created_by_principal=self.owner,
        )
        TaskContractPolicyLink.objects.create(
            task_contract_version=self.contract, policy_version=self.policy_version,
            required=True, created_by_principal=self.owner,
        )
        self.task = Task.objects.create(
            product=self.product, product_profile_version=self.profile, contract_version=self.contract,
            title="Publish PUKO video",
            created_by_principal=self.owner, updated_by_principal=self.owner,
        )
        TaskCheckRun.record_completed(
            task=self.task, check_kind=TaskCheckRun.Kind.DOR,
            results=[{
                "criterion_key": "input",
                "result": TaskCheckRun.Result.PASS,
                "evidence": {"ready": True},
            }],
            command_id=uuid.uuid4(), evaluator_principal=self.owner,
            acting_role=ActingRole.OWNER, permission_grant=self.edit_grant,
            recorded_by_principal=self.owner,
        )
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.READY, command_id=uuid.uuid4(),
            expected_state_version=0, actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.edit_grant, recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        TaskAssignment.record(
            task=self.task, assignee_principal=self.owner, command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version, assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER, permission_grant=self.assign_grant,
            recorded_by_principal=self.owner,
        )
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.ASSIGNED, command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version, actor_principal=self.owner,
            acting_role=ActingRole.OWNER, permission_grant=self.assign_grant,
            recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.IN_PROGRESS, command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version, actor_principal=self.owner,
            acting_role=ActingRole.OWNER, permission_grant=self.edit_grant,
            recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        self.dod_run = TaskCheckRun.record_completed(
            task=self.task, check_kind=TaskCheckRun.Kind.DOD,
            results=[{
                "criterion_key": "deliverable",
                "result": TaskCheckRun.Result.PASS,
                "evidence": {"asset_ready": True},
            }],
            command_id=uuid.uuid4(), evaluator_principal=self.owner,
            acting_role=ActingRole.OWNER, permission_grant=self.edit_grant,
            recorded_by_principal=self.owner,
        )
        self.asset = ContentAsset.create_idempotent(
            task=self.task, asset_key="primary", title="Primary copy", asset_kind=ContentAsset.AssetKind.COPY,
            command_id=uuid.uuid4(), actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.edit_grant, recorded_by_principal=self.owner,
        )
        self.asset_version = ContentAssetVersion.create_next(
            content_asset=self.asset, object_key="staging/puko/copy-v1.txt", mime_type="text/plain",
            byte_size=42, content_sha256="a" * 64, command_id=uuid.uuid4(),
            actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.edit_grant, recorded_by_principal=self.owner,
        )
        self.submission = TaskSubmission.seal(
            task=self.task, dod_check_run=self.dod_run, primary_asset_version=self.asset_version,
            command_id=uuid.uuid4(), expected_task_version=self.task.state_version,
            actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.edit_grant, recorded_by_principal=self.owner,
        )
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.SUBMITTED, command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version, actor_principal=self.owner,
            acting_role=ActingRole.OWNER, permission_grant=self.edit_grant,
            recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.UNDER_REVIEW, command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version, actor_principal=self.owner,
            acting_role=ActingRole.OWNER, permission_grant=self.edit_grant,
            recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        self.review = ReviewDecision.record_final(
            submission=self.submission, decision=ReviewDecision.Decision.APPROVED,
            rationale="Approved for deterministic release evaluation.", command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version, reviewer_principal=self.reviewer,
            acting_role=ActingRole.OPERATIONS_ADMIN, permission_grant=self.review_grant,
            recorded_by_principal=self.reviewer,
        )
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.APPROVED, command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version, actor_principal=self.owner,
            acting_role=ActingRole.OWNER, permission_grant=self.edit_grant,
            recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        self.publication = Publication.objects.create_intent(
            submission=self.submission, command_id=uuid.uuid4(), payload_hash=digest("publication-intent"),
            actor_principal=self.publisher, acting_role=ActingRole.OPERATOR,
            permission_grant=self.publish_grant, recorded_by_principal=self.publisher,
        )
        self.channel_account = ChannelAccount.objects.create(
            platform_code="TIKTOK", account_code="puko-us", external_account_ref="puko-us-public",
            display_name="PUKO US", created_by_principal=self.owner, updated_by_principal=self.owner,
        )
        self.environment = RuntimeEnvironment.objects.create(
            environment_code="staging-us", environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="staging-identity", database_namespace="staging-db",
            object_storage_namespace="staging-objects", created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.binding = AccountEnvironmentBinding.objects.create(
            channel_account=self.channel_account, runtime_environment=self.environment, binding_version=1,
            identity_reference="staging-publisher-identity", valid_from=now - timedelta(minutes=5),
            created_by_principal=self.owner, recorded_by_principal=self.owner,
        )
        self.capability = CapabilityState.objects.create(
            account_environment_binding=self.binding, state_version=1,
            capability_code=CapabilityState.MANUAL_PUBLISH, state=CapabilityState.State.OPEN,
            effective_from=now - timedelta(minutes=5), created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )

    def _grant(self, principal, action):
        return PermissionGrant.objects.create(
            principal=principal, scope_kind=PermissionGrant.ScopeKind.GLOBAL, action=action,
            effect=PermissionGrant.Effect.ALLOW, valid_from=timezone.now() - timedelta(minutes=5),
            valid_until=timezone.now() + timedelta(hours=2), granted_by_principal=self.owner,
        )

    def _gate(self, capability=None, result=RuleEvaluationResult.Result.PASS):
        capability = capability or self.capability
        policies = [self.policy_version]
        context_hash = self._context_hash(capability)
        run = RuleEvaluationRun.objects.start(
            publication=self.publication, policy_versions=policies, context_hash=context_hash,
            evaluator_key="deterministic-v1", command_id=uuid.uuid4(), payload_hash=digest("rule-run"),
            initiated_by_principal=self.rule_evaluator, acting_role=ActingRole.SYSTEM,
            permission_grant=self.rule_evaluation_grant, recorded_by_principal=self.rule_evaluator,
        )
        RuleEvaluationResult.objects.record(
            evaluation_run=run, policy_version=self.policy_version, rule_code="exact_release_context",
            required=True, result=result, command_id=uuid.uuid4(), payload_hash=digest("rule-result"),
            evaluated_by_principal=self.rule_evaluator, recorded_by_principal=self.rule_evaluator,
        )
        run.complete()
        gate = ReleaseGateRecord.objects.evaluate(
            publication=self.publication, task_submission=self.submission, review_decision=self.review,
            primary_asset_version=self.asset_version, task_contract_version=self.contract,
            publisher_principal=self.publisher, publisher_grant=self.publish_grant,
            channel_account=self.channel_account, runtime_environment=self.environment,
            account_environment_binding=self.binding, capability_state=capability, evaluation_run=run,
            command_id=uuid.uuid4(), payload_hash=digest("release-gate"),
            evaluated_by_principal=self.publisher, evaluated_by_acting_role=ActingRole.OPERATOR,
            recorded_by_principal=self.publisher,
        )
        return run, gate

    def _context_hash(self, capability=None):
        capability = capability or self.capability
        return release_context_hash(
            publication=self.publication, review_decision=self.review,
            primary_asset_version=self.asset_version, task_contract_version=self.contract,
            publisher_principal=self.publisher, publisher_grant=self.publish_grant,
            channel_account=self.channel_account, runtime_environment=self.environment,
            account_environment_binding=self.binding, capability_state=capability,
            policy_versions=[self.policy_version],
        )

    def _append(self, event_type, expected_version, gate=None, **proof):
        return self.publication.append_event(
            event_type=event_type, release_gate=gate, command_id=uuid.uuid4(),
            payload_hash=digest(event_type), expected_state_version=expected_version,
            actor_principal=self.publisher, acting_role=ActingRole.OPERATOR,
            permission_grant=self.publish_grant, recorded_by_principal=self.publisher, **proof,
        )

    def test_publication_starts_gate_pending_and_manual_path_is_event_only(self):
        self.assertEqual(self.publication.status, Publication.Status.GATE_PENDING)
        self.assertEqual(self.publication.state_version, 1)
        self.assertEqual(list(self.publication.events.values_list("event_type", flat=True)), ["GATE_PENDING"])
        _, gate = self._gate()
        self.assertEqual(gate.outcome, ReleaseGateRecord.Outcome.PASSED)
        self.assertEqual(gate.evaluation_links.count(), 1)
        ready = self._append(PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH, 1, gate)
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)
        published = self._append(
            PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED, 2, gate,
            external_publication_id="external-123", external_url="https://example.com/p/123",
        )
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.status, Publication.Status.MANUAL_PUBLISHED_RECORDED)
        self.assertEqual(published.previous_event_id, ready.pk)
        published.external_publication_id = "rewritten"
        with self.assertRaises(ValidationError):
            published.save()

    def test_fail_closed_for_closed_capability_and_nonpassing_rule(self):
        closed = CapabilityState.objects.create(
            account_environment_binding=self.binding, capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=2, state=CapabilityState.State.CLOSED, supersedes=self.capability,
            reason="Emergency stop", created_by_principal=self.owner, recorded_by_principal=self.owner,
        )
        _, gate = self._gate(capability=closed, result=RuleEvaluationResult.Result.FAIL)
        self.assertEqual(gate.outcome, ReleaseGateRecord.Outcome.BLOCKED)
        self.assertIn("MANUAL_PUBLISH_CAPABILITY_NOT_OPEN", gate.failure_reasons)
        self.assertIn("RULE_EVALUATION_NOT_PASSED", gate.failure_reasons)
        with self.assertRaises(ValidationError):
            self._append(PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH, 1, gate)
        self.assertEqual(self.publication.events.count(), 1)

    def test_old_gate_cannot_be_reused_after_return_to_pending(self):
        _, gate = self._gate()
        self._append(PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH, 1, gate)
        self.publication.refresh_from_db()
        self._append(PublicationEvent.EventType.GATE_PENDING, 2, gate)
        self.publication.refresh_from_db()
        with self.assertRaises(ValidationError):
            self._append(PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH, 3, gate)
        self.assertEqual(self.publication.events.count(), 3)

    def test_versions_results_gates_and_events_are_immutable(self):
        result_run, gate = self._gate()
        result = result_run.results.get()
        for fact, field, value in [
            (self.policy_version, "version_number", 2),
            (result, "result", RuleEvaluationResult.Result.FAIL),
            (gate, "outcome", ReleaseGateRecord.Outcome.BLOCKED),
        ]:
            setattr(fact, field, value)
            with self.assertRaises(ValidationError):
                fact.save()

    def test_last_mile_recomputes_context_even_when_account_remains_active(self):
        _, gate = self._gate()
        self.channel_account.display_name = "PUKO US renamed"
        self.channel_account.save()
        self.assertEqual(self.channel_account.status, ChannelAccount.Status.ACTIVE)
        self.assertIn("RELEASE_CONTEXT_CHANGED", gate.current_blockers())
        with self.assertRaises(ValidationError):
            self._append(PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH, 1, gate)
        self.assertEqual(self.publication.events.count(), 1)

    def test_rule_result_command_replay_is_idempotent_and_payload_conflict_is_rejected(self):
        run = RuleEvaluationRun.objects.start(
            publication=self.publication, policy_versions=[self.policy_version],
            context_hash=self._context_hash(), evaluator_key="deterministic-v1",
            command_id=uuid.uuid4(), payload_hash=digest("idempotent-run"),
            initiated_by_principal=self.rule_evaluator, acting_role=ActingRole.SYSTEM,
            permission_grant=self.rule_evaluation_grant, recorded_by_principal=self.rule_evaluator,
        )
        command_id = uuid.uuid4()
        payload_hash = digest("idempotent-result")
        values = {
            "evaluation_run": run,
            "policy_version": self.policy_version,
            "rule_code": "exact_release_context",
            "required": True,
            "result": RuleEvaluationResult.Result.PASS,
            "command_id": command_id,
            "payload_hash": payload_hash,
            "evaluated_by_principal": self.rule_evaluator,
            "recorded_by_principal": self.rule_evaluator,
        }
        first = RuleEvaluationResult.objects.record(**values)
        replay = RuleEvaluationResult.objects.record(**values)
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(run.results.count(), 1)
        with self.assertRaises(ValidationError):
            RuleEvaluationResult.objects.record(**{**values, "payload_hash": digest("different-result")})
        self.assertEqual(run.results.count(), 1)

    def test_publisher_cannot_start_or_write_its_own_rule_evaluation(self):
        with self.assertRaises(ValidationError):
            RuleEvaluationRun.objects.start(
                publication=self.publication,
                policy_versions=[self.policy_version],
                context_hash=self._context_hash(),
                evaluator_key="publisher-self-attestation",
                command_id=uuid.uuid4(),
                payload_hash=digest("publisher-run"),
                initiated_by_principal=self.publisher,
                acting_role=ActingRole.OPERATOR,
                permission_grant=self.publish_grant,
                recorded_by_principal=self.publisher,
            )

        run = RuleEvaluationRun.objects.start(
            publication=self.publication,
            policy_versions=[self.policy_version],
            context_hash=self._context_hash(),
            evaluator_key="deterministic-v1",
            command_id=uuid.uuid4(),
            payload_hash=digest("trusted-run"),
            initiated_by_principal=self.rule_evaluator,
            acting_role=ActingRole.SYSTEM,
            permission_grant=self.rule_evaluation_grant,
            recorded_by_principal=self.rule_evaluator,
        )
        with self.assertRaises(ValidationError):
            RuleEvaluationResult.objects.record(
                evaluation_run=run,
                policy_version=self.policy_version,
                rule_code="exact_release_context",
                required=True,
                result=RuleEvaluationResult.Result.PASS,
                command_id=uuid.uuid4(),
                payload_hash=digest("publisher-result"),
                evaluated_by_principal=self.publisher,
                recorded_by_principal=self.publisher,
            )
        self.assertEqual(run.results.count(), 0)

    def test_gate_blocks_if_a_required_exact_contract_policy_is_missing_from_completed_run(self):
        run = RuleEvaluationRun.objects.start(
            publication=self.publication,
            policy_versions=[self.policy_version],
            context_hash=self._context_hash(),
            evaluator_key="deterministic-v1",
            command_id=uuid.uuid4(),
            payload_hash=digest("pre-link-run"),
            initiated_by_principal=self.rule_evaluator,
            acting_role=ActingRole.SYSTEM,
            permission_grant=self.rule_evaluation_grant,
            recorded_by_principal=self.rule_evaluator,
        )
        RuleEvaluationResult.objects.record(
            evaluation_run=run,
            policy_version=self.policy_version,
            rule_code="exact_release_context",
            required=True,
            result=RuleEvaluationResult.Result.PASS,
            command_id=uuid.uuid4(),
            payload_hash=digest("pre-link-result"),
            evaluated_by_principal=self.rule_evaluator,
            recorded_by_principal=self.rule_evaluator,
        )
        run.complete()

        contract_definition = PolicyDefinition.objects.create(
            policy_code="CONTRACT_EXACT_ONLY",
            name="Contract exact policy",
            is_mandatory=False,
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        contract_policy = PolicyVersion.objects.create(
            policy_definition=contract_definition,
            version_number=1,
            rules=[{"rule_code": "contract_exact_rule", "required": True}],
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        TaskContractPolicyLink.objects.create(
            task_contract_version=self.contract,
            policy_version=contract_policy,
            required=True,
            created_by_principal=self.owner,
        )

        gate = ReleaseGateRecord.objects.evaluate(
            publication=self.publication,
            task_submission=self.submission,
            review_decision=self.review,
            primary_asset_version=self.asset_version,
            task_contract_version=self.contract,
            publisher_principal=self.publisher,
            publisher_grant=self.publish_grant,
            channel_account=self.channel_account,
            runtime_environment=self.environment,
            account_environment_binding=self.binding,
            capability_state=self.capability,
            evaluation_run=run,
            command_id=uuid.uuid4(),
            payload_hash=digest("missing-contract-policy-gate"),
            evaluated_by_principal=self.rule_evaluator,
            evaluated_by_acting_role=ActingRole.SYSTEM,
            recorded_by_principal=self.rule_evaluator,
        )
        self.assertEqual(gate.outcome, ReleaseGateRecord.Outcome.BLOCKED)
        self.assertIn("REQUIRED_POLICY_SET_MISSING_OR_STALE", gate.failure_reasons)

    def test_current_policy_version_cannot_replace_contracts_exact_version(self):
        current_v2 = PolicyVersion.objects.create(
            policy_definition=self.policy_definition,
            version_number=2,
            rules=[{"rule_code": "exact_release_context", "required": True}],
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        # The exact contract snapshot still requires v1 while the current
        # mandatory set now requires v2.  Both must be evaluated.
        with self.assertRaises(ValidationError):
            RuleEvaluationRun.objects.start(
                publication=self.publication,
                policy_versions=[current_v2],
                context_hash=digest("wrong-policy-context"),
                evaluator_key="deterministic-v1",
                command_id=uuid.uuid4(),
                payload_hash=digest("wrong-policy-run"),
                initiated_by_principal=self.rule_evaluator,
                acting_role=ActingRole.SYSTEM,
                permission_grant=self.rule_evaluation_grant,
                recorded_by_principal=self.rule_evaluator,
            )

    def test_v1_service_orchestrates_ready_then_records_only_manual_proof_idempotently(self):
        gate_command = uuid.uuid4()
        first = orchestrate_v1_release_gate(
            task=self.task,
            submission=self.submission,
            publisher_principal=self.publisher,
            channel_account=self.channel_account,
            runtime_environment=self.environment,
            command_id=gate_command,
        )
        replay = orchestrate_v1_release_gate(
            task=self.task,
            submission=self.submission,
            publisher_principal=self.publisher,
            channel_account=self.channel_account,
            runtime_environment=self.environment,
            command_id=gate_command,
        )
        self.assertEqual(first.gate.pk, replay.gate.pk)
        self.assertEqual(first.gate.outcome, ReleaseGateRecord.Outcome.PASSED)
        self.assertEqual(first.publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)
        self.assertEqual(first.evaluation_run.initiated_by_principal_id, self.rule_evaluator.pk)
        self.assertEqual(first.evaluation_run.initiated_by_acting_role, ActingRole.SYSTEM)
        self.assertEqual(first.evaluation_run.results.get().result, RuleEvaluationResult.Result.PASS)
        self.assertEqual(first.publication.gate_records.count(), 1)
        self.assertEqual(first.publication.evaluation_runs.count(), 1)

        proof_command = uuid.uuid4()
        proof = record_manual_publication_proof(
            publication=first.publication,
            publisher_principal=self.publisher,
            command_id=proof_command,
            external_url="https://www.tiktok.com/@puko/video/123",
            external_publication_id="tiktok-123",
        )
        proof_replay = record_manual_publication_proof(
            publication=first.publication,
            publisher_principal=self.publisher,
            command_id=proof_command,
            external_url="https://www.tiktok.com/@puko/video/123",
            external_publication_id="tiktok-123",
        )
        self.assertEqual(proof.pk, proof_replay.pk)
        first.publication.refresh_from_db()
        self.assertEqual(first.publication.status, Publication.Status.MANUAL_PUBLISHED_RECORDED)
        self.assertEqual(
            first.publication.events.filter(
                event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED
            ).count(),
            1,
        )

    def test_v1_service_unknown_required_rule_is_error_and_blocks_ready(self):
        unknown_definition = PolicyDefinition.objects.create(
            policy_code="UNKNOWN_REQUIRED_V1",
            name="Unknown required test rule",
            is_mandatory=True,
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        unknown_policy = PolicyVersion.objects.create(
            policy_definition=unknown_definition,
            version_number=1,
            rules=[{"rule_code": "future_unknown_required", "required": True}],
            effective_from=timezone.now() - timedelta(minutes=1),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        result = orchestrate_v1_release_gate(
            task=self.task,
            submission=self.submission,
            publisher_principal=self.publisher,
            channel_account=self.channel_account,
            runtime_environment=self.environment,
            command_id=uuid.uuid4(),
        )
        unknown_result = result.evaluation_run.results.get(
            policy_version=unknown_policy,
            rule_code="future_unknown_required",
        )
        self.assertEqual(unknown_result.result, RuleEvaluationResult.Result.ERROR)
        self.assertEqual(unknown_result.detail["error"], "UNKNOWN_REQUIRED_RULE")
        self.assertEqual(result.gate.outcome, ReleaseGateRecord.Outcome.BLOCKED)
        self.assertEqual(result.publication.status, Publication.Status.GATE_BLOCKED)
        self.assertEqual(result.terminal_event.event_type, PublicationEvent.EventType.GATE_BLOCKED)
        self.assertFalse(
            result.publication.events.filter(
                event_type=PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH
            ).exists()
        )

    def test_v1_service_closed_capability_creates_complete_blocked_chain(self):
        closed = CapabilityState.objects.create(
            account_environment_binding=self.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=2,
            state=CapabilityState.State.CLOSED,
            reason="Test safety stop",
            supersedes=self.capability,
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        result = orchestrate_v1_release_gate(
            task=self.task,
            submission=self.submission,
            publisher_principal=self.publisher,
            channel_account=self.channel_account,
            runtime_environment=self.environment,
            command_id=uuid.uuid4(),
        )
        self.assertEqual(result.gate.capability_state_id, closed.pk)
        self.assertEqual(result.gate.outcome, ReleaseGateRecord.Outcome.BLOCKED)
        self.assertIn("MANUAL_PUBLISH_CAPABILITY_NOT_OPEN", result.gate.failure_reasons)
        self.assertEqual(result.evaluation_run.status, RuleEvaluationRun.Status.COMPLETED)
        self.assertEqual(result.terminal_event.event_type, PublicationEvent.EventType.GATE_BLOCKED)

    def test_v1_service_missing_evaluator_rolls_back_without_half_gate_or_event(self):
        self.rule_evaluation_grant.grant_status = PermissionGrant.GrantStatus.REVOKED
        self.rule_evaluation_grant.save()
        original_event_count = self.publication.events.count()
        with self.assertRaises(ValidationError):
            orchestrate_v1_release_gate(
                task=self.task,
                submission=self.submission,
                publisher_principal=self.publisher,
                channel_account=self.channel_account,
                runtime_environment=self.environment,
                command_id=uuid.uuid4(),
            )
        self.assertEqual(self.publication.events.count(), original_event_count)
        self.assertEqual(RuleEvaluationRun.objects.count(), 0)
        self.assertEqual(RuleEvaluationResult.objects.count(), 0)
        self.assertEqual(ReleaseGateRecord.objects.count(), 0)

    def test_v1_service_expired_publish_grant_before_gate_leaves_zero_half_facts(self):
        self.publish_grant.valid_until = timezone.now() - timedelta(seconds=1)
        self.publish_grant.save(update_fields=["valid_until", "updated_at"])
        before_publications = Publication.objects.count()
        before_events = PublicationEvent.objects.count()

        with self.assertRaises(ValidationError):
            orchestrate_v1_release_gate(
                task=self.task,
                submission=self.submission,
                publisher_principal=self.publisher,
                channel_account=self.channel_account,
                runtime_environment=self.environment,
                command_id=uuid.uuid4(),
            )

        self.assertEqual(Publication.objects.count(), before_publications)
        self.assertEqual(PublicationEvent.objects.count(), before_events)
        self.assertEqual(RuleEvaluationRun.objects.count(), 0)
        self.assertEqual(RuleEvaluationResult.objects.count(), 0)
        self.assertEqual(ReleaseGateRecord.objects.count(), 0)

    def test_v1_service_unauthorized_publisher_and_link_only_proof(self):
        unauthorized = Principal.objects.create_user(username="unauthorized-publisher")
        original_publication_count = Publication.objects.count()
        original_event_count = PublicationEvent.objects.count()
        with self.assertRaises(ValidationError):
            orchestrate_v1_release_gate(
                task=self.task,
                submission=self.submission,
                publisher_principal=unauthorized,
                channel_account=self.channel_account,
                runtime_environment=self.environment,
                command_id=uuid.uuid4(),
            )
        self.assertEqual(Publication.objects.count(), original_publication_count)
        self.assertEqual(PublicationEvent.objects.count(), original_event_count)
        self.assertEqual(ReleaseGateRecord.objects.count(), 0)

        ready = orchestrate_v1_release_gate(
            task=self.task,
            submission=self.submission,
            publisher_principal=self.publisher,
            channel_account=self.channel_account,
            runtime_environment=self.environment,
            command_id=uuid.uuid4(),
        )
        before_proof = ready.publication.events.count()
        with self.assertRaises(ValidationError):
            record_manual_publication_proof(
                publication=ready.publication,
                publisher_principal=self.publisher,
                command_id=uuid.uuid4(),
            )
        self.assertEqual(ready.publication.events.count(), before_proof)

        for forbidden_file_proof in (
            {"proof_reference": "proofs/legacy.png"},
            {"proof_sha256": "d" * 64},
            {"proof_reference": "proofs/legacy.png", "proof_sha256": "d" * 64},
        ):
            with self.subTest(forbidden_file_proof=forbidden_file_proof):
                with self.assertRaises(ValidationError):
                    ready.publication.append_event(
                        event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED,
                        release_gate=ready.gate,
                        command_id=uuid.uuid4(),
                        payload_hash=canonical_sha256(forbidden_file_proof),
                        expected_state_version=ready.publication.state_version,
                        actor_principal=self.publisher,
                        acting_role=self.publisher.role,
                        permission_grant=self.publish_grant,
                        recorded_by_principal=self.publisher,
                        external_url="https://example.com/published",
                        **forbidden_file_proof,
                    )
                self.assertEqual(ready.publication.events.count(), before_proof)

        proof = record_manual_publication_proof(
            publication=ready.publication,
            publisher_principal=self.publisher,
            command_id=uuid.uuid4(),
            external_url="https://example.com/published",
        )
        self.assertEqual(proof.external_url, "https://example.com/published")
        self.assertEqual(proof.proof_reference, "")
        self.assertEqual(proof.proof_sha256, "")
        self.assertEqual(ready.publication.events.count(), before_proof + 1)

    def test_v1_service_hierarchical_publish_deny_overrides_allow_without_partial_facts(self):
        PermissionGrant.objects.create(
            principal=self.publisher,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.PUBLISH,
            effect=PermissionGrant.Effect.DENY,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        before_publications = Publication.objects.count()
        before_events = PublicationEvent.objects.count()
        with self.assertRaises(ValidationError):
            orchestrate_v1_release_gate(
                task=self.task,
                submission=self.submission,
                publisher_principal=self.publisher,
                channel_account=self.channel_account,
                runtime_environment=self.environment,
                command_id=uuid.uuid4(),
            )
        self.assertEqual(Publication.objects.count(), before_publications)
        self.assertEqual(PublicationEvent.objects.count(), before_events)
        self.assertEqual(RuleEvaluationRun.objects.count(), 0)
        self.assertEqual(ReleaseGateRecord.objects.count(), 0)
