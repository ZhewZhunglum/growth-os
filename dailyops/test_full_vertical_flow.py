from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from contentops.models import (
    ContentAsset,
    ContentAssetVersion,
    ReviewDecision,
    TaskSubmission,
)
from dailyops.runtime import (
    ConnectorBatchResult,
    DailyOperationsRuntime,
    build_daily_operations_runtime,
)
from dailyops.services import (
    accept_daily_analysis,
    compile_channel_plan_task,
    create_initiative_from_opportunity,
    ensure_default_sources,
    propose_daily_analysis,
    run_automatic_collection,
    start_daily_batch,
)
from feedbackui.services import (
    add_geo_panel_item,
    create_geo_panel_version,
    propose_learning,
    record_geo_result,
    record_performance_rows,
)
from governance.models import (
    Issue,
    Meeting,
    MeetingDecision,
    PolicyActivation,
    RuleProposalSourceLink,
    RuleProposalVersion,
)
from governanceui.services import (
    create_issue,
    create_meeting,
    create_meeting_decision,
    create_rule_proposal,
)
from insights.models import (
    AvailabilityState,
    DataDomain,
    GEOMetricObservation,
    LearningEvidenceLink,
    LearningVersion,
    PublicationPerformanceObservation,
)
from integrations.connectors.types import (
    AcquisitionMode,
    ConnectorResult,
    ConnectorRunStatus,
    Platform,
)
from integrations.publishing import PublicationDispatchStatus, PublicationMode
from intelligence.models import (
    ChannelPlan,
    CollectionRun,
    EvidenceArtifactLink,
    ExternalEvidenceItem,
    Initiative,
    ProductOpportunity,
    RawArtifact,
    SignalAssessment,
    TaskCompilationContext,
)
from intelligence.services import (
    transition_channel_plan,
    transition_initiative,
    transition_opportunity,
)
from products.models import (
    ClaimMatrixVersion,
    EvidenceLibraryVersion,
    ObjectiveProfileVersion,
    Product,
    ProductProfileVersion,
)
from releasegate.models import (
    AccountEnvironmentBinding,
    CapabilityState,
    ChannelAccount,
    PolicyDefinition,
    PolicyVersion,
    Publication,
    PublicationEvent,
    ReleaseGateRecord,
    RuntimeEnvironment,
)
from releasegate.publishing import (
    dispatch_confirmed_publication,
    prepare_human_publication_confirmation,
)
from releasegate.services import orchestrate_v1_release_gate
from workflow.models import (
    Task,
    TaskAssignment,
    TaskCheckRun,
    TaskContractPolicyLink,
    TaskContractVersion,
)


class _OfflineSevenPlatformRunner:
    """One deterministic success plus six explicit browser blockers."""

    def __init__(self):
        self.calls = 0

    def run(self, requests):
        self.calls += 1
        results = {}
        for platform, request in requests.items():
            if platform is Platform.PINTEREST:
                results[platform] = ConnectorResult(
                    platform=platform,
                    status=ConnectorRunStatus.SUCCEEDED,
                    operation_key=request.operation_key,
                    mode=AcquisitionMode.API,
                    provider="offline-fake-pinterest-api",
                    items=(
                        {
                            "external_id": "pin-offline-daily-v1",
                            "url": "https://www.pinterest.com/pin/offline-daily-v1/",
                            "title": "People are looking for an afternoon focus routine",
                            "content_text": (
                                "A bounded offline fixture showing demand for a simple "
                                "afternoon focus routine."
                            ),
                            "attributes": {"fixture": "daily-operations-v1"},
                        },
                    ),
                    provenance=({"transport": "offline-fake", "network": False},),
                )
            else:
                results[platform] = ConnectorResult(
                    platform=platform,
                    status=ConnectorRunStatus.BLOCKED,
                    operation_key=request.operation_key,
                    mode=AcquisitionMode.BROWSER,
                    provider="offline-unpaired-browser",
                    reason="Offline integration fixture has no paired browser worker.",
                )
        return ConnectorBatchResult(results)


class FullDailyOperationsVerticalFlowTests(TestCase):
    """Exercise the V1 business chain without network, server or file upload."""

    def setUp(self):
        self.now = timezone.now()
        self.owner = Principal.objects.create_user(
            username="vertical-owner",
            password="safe-test-password-123",
            role=Principal.Role.OWNER,
        )
        self.operator = Principal.objects.create_user(
            username="vertical-operator",
            password="safe-test-password-123",
            role=Principal.Role.OPERATOR,
        )
        self.reviewer = Principal.objects.create_user(
            username="vertical-reviewer",
            password="safe-test-password-123",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.publisher = Principal.objects.create_user(
            username="vertical-publisher",
            password="safe-test-password-123",
            role=Principal.Role.OPERATOR,
        )
        self.evaluator = Principal.objects.create_user(
            username="vertical-rule-evaluator",
            role=Principal.Role.OPERATOR,
            principal_type=Principal.PrincipalType.SERVICE_ACCOUNT,
        )
        self.product = Product.objects.create(
            product_code="PUKO-VERTICAL-V1",
            name="PUKO Daily Operations V1",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self._create_permissions()
        ensure_default_sources(principal=self.owner, acting_role=self.owner.role)
        self._create_product_and_release_context()

    def _grant(
        self,
        principal,
        action,
        *,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=None,
        account_ref="",
        risk_level=PermissionGrant.RiskLevel.LOW,
    ):
        return PermissionGrant.objects.create(
            principal=principal,
            scope_kind=scope_kind,
            product=(product if scope_kind == PermissionGrant.ScopeKind.PRODUCT else None),
            account_ref=(account_ref if scope_kind == PermissionGrant.ScopeKind.ACCOUNT else ""),
            action=action,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=risk_level,
            valid_from=self.now - timedelta(minutes=5),
            valid_until=self.now + timedelta(days=1),
            granted_by_principal=self.owner,
        )

    def _create_permissions(self):
        self.owner_grants = {}
        for action in (
            PermissionGrant.Action.VIEW,
            PermissionGrant.Action.EDIT,
            PermissionGrant.Action.CREATE_TASK,
            PermissionGrant.Action.ASSIGN_TASK,
            PermissionGrant.Action.COMPLETE_TASK,
            PermissionGrant.Action.COLLECT_READ_ONLY,
        ):
            self.owner_grants[action] = self._grant(
                self.owner,
                action,
                product=self.product,
            )
        for action in (
            PermissionGrant.Action.MANAGE_ACCOUNT,
            PermissionGrant.Action.COLLECT_READ_ONLY,
            PermissionGrant.Action.EDIT,
            PermissionGrant.Action.APPROVE,
        ):
            self.owner_grants[f"global:{action}"] = self._grant(
                self.owner,
                action,
                scope_kind=PermissionGrant.ScopeKind.GLOBAL,
                risk_level=(
                    PermissionGrant.RiskLevel.HIGH
                    if action == PermissionGrant.Action.MANAGE_ACCOUNT
                    else PermissionGrant.RiskLevel.LOW
                ),
            )
        self.operator_edit = self._grant(
            self.operator,
            PermissionGrant.Action.EDIT,
            product=self.product,
        )
        self.operator_review = self._grant(
            self.operator,
            PermissionGrant.Action.REVIEW,
            product=self.product,
        )
        self.reviewer_grant = self._grant(
            self.reviewer,
            PermissionGrant.Action.REVIEW,
            product=self.product,
        )
        self.evaluator_grant = self._grant(
            self.evaluator,
            PermissionGrant.Action.REVIEW,
            product=self.product,
        )

    def _create_product_and_release_context(self):
        objective = ObjectiveProfileVersion.objects.create(
            objective_key="VERTICAL_DAILY_V1",
            version_number=1,
            primary_objectives=["reach"],
            secondary_objectives=["engagement"],
            retained_metrics=["purchase"],
            priority_rules={"reach": 1},
            strategy_boundaries={"human_confirmation": True},
            created_by_principal=self.owner,
        )
        objective.seal(principal=self.owner)
        claims = ClaimMatrixVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
        )
        claims.seal(principal=self.owner)
        evidence = EvidenceLibraryVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
        )
        evidence.seal(principal=self.owner)
        profile = ProductProfileVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"intent": "afternoon focus"},
            core_value_proposition="Evidence-informed daily focus support.",
            brand_voice={"tone": "clear and measured"},
            product_facts={"format": "supplement"},
            prohibited_expressions=["cure"],
            objective_profile_version=objective,
            claim_matrix_version=claims,
            evidence_library_version=evidence,
            created_by_principal=self.owner,
        )
        profile.seal(self.owner)
        self.product.current_profile_version = profile
        self.product.save(update_fields=["current_profile_version", "updated_at"])
        self.profile = profile

        self.contract = TaskContractVersion.objects.create(
            product_profile_version=profile,
            version_number=1,
            title="Daily Operations link-only publication",
            dor_criteria=[{"key": "source_ready", "required": True}],
            dod_criteria=[{"key": "external_link", "required": True}],
            release_gate_criteria=[{"key": "exact_release_context", "required": True}],
            success_criteria=[{"key": "published_url", "required": True}],
            sealed_at=self.now,
            created_by_principal=self.owner,
        )
        self.policy_definition = PolicyDefinition.objects.create(
            policy_code="VERTICAL-V1-RELEASE",
            name="Daily Operations exact release context",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.release_policy = PolicyVersion.objects.create(
            policy_definition=self.policy_definition,
            version_number=1,
            rules=[{"rule_code": "exact_release_context", "required": True}],
            effective_from=self.now - timedelta(minutes=5),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        TaskContractPolicyLink.objects.create(
            task_contract_version=self.contract,
            policy_version=self.release_policy,
            required=True,
            created_by_principal=self.owner,
        )

        self.account = ChannelAccount.objects.create(
            platform_code=Platform.TIKTOK.value,
            account_code="vertical-puko-tiktok",
            external_account_ref="@puko-vertical",
            display_name="PUKO Vertical TikTok",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.environment = RuntimeEnvironment.objects.create(
            environment_code="vertical-offline-staging",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="vertical-offline",
            database_namespace="vertical-test-db",
            object_storage_namespace="link-only-no-upload",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.binding = AccountEnvironmentBinding.objects.create(
            channel_account=self.account,
            runtime_environment=self.environment,
            binding_version=1,
            identity_reference="offline-browser-profile",
            valid_from=self.now - timedelta(minutes=5),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.capability = CapabilityState.objects.create(
            account_environment_binding=self.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=1,
            state=CapabilityState.State.OPEN,
            effective_from=self.now - timedelta(minutes=5),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.publish_grant = self._grant(
            self.publisher,
            PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref=self.account.account_code,
            risk_level=PermissionGrant.RiskLevel.HIGH,
        )
        self.account_collect_grant = self._grant(
            self.owner,
            PermissionGrant.Action.COLLECT_READ_ONLY,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref=self.account.account_code,
        )

    def _transition_intelligence(self, service, *, aggregate, to_state, expected_version, reason):
        field = {
            transition_opportunity: "opportunity_id",
            transition_initiative: "initiative_id",
            transition_channel_plan: "channel_plan_id",
        }[service]
        return service(
            **{
                field: aggregate.pk,
                "to_state": to_state,
                "expected_version": expected_version,
                "command_id": uuid.uuid4(),
                "reason": reason,
                "principal": self.owner,
                "acting_role": self.owner.role,
            }
        ).aggregate

    def _transition_task(self, task, to_state, *, actor, grant, reason):
        Task.transition(
            task_id=task.pk,
            to_state=to_state,
            command_id=uuid.uuid4(),
            expected_state_version=task.state_version,
            actor_principal=actor,
            acting_role=actor.role,
            permission_grant=grant,
            recorded_by_principal=actor,
            reason=reason,
        )
        task.refresh_from_db()

    def test_offline_daily_operations_v1_reaches_learning_and_proposal_only(self):
        # Seven-platform collection: one provenance-linked API fact and six
        # explicit browser blockers. No route performs network I/O.
        batch_key = uuid.uuid4()
        start_daily_batch(
            batch_key=batch_key,
            product=self.product,
            query="afternoon focus routine",
            window_start=self.now - timedelta(days=7),
            window_end=self.now,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        runner = _OfflineSevenPlatformRunner()
        base_runtime = build_daily_operations_runtime()
        runtime = DailyOperationsRuntime(
            ai_provider=base_runtime.ai_provider,
            connectors=runner,
            live_ai_enabled=False,
            ai_model=base_runtime.ai_model,
        )
        automatic = run_automatic_collection(
            batch_key=batch_key,
            product=self.product,
            command_id=uuid.uuid4(),
            principal=self.owner,
            acting_role=self.owner.role,
            runtime=runtime,
        )
        self.assertEqual(runner.calls, 1)
        self.assertEqual(len(automatic.runs), 7)
        self.assertEqual(
            {run.source.platform_code for run in automatic.runs},
            {platform.value for platform in Platform},
        )
        self.assertEqual(
            sum(run.status == CollectionRun.Status.SUCCEEDED for run in automatic.runs),
            1,
        )
        self.assertEqual(
            sum(run.status == CollectionRun.Status.BLOCKED for run in automatic.runs),
            6,
        )
        evidence_item = ExternalEvidenceItem.objects.get(collection_run__in=automatic.runs)
        raw_artifact = RawArtifact.objects.get(collection_run=evidence_item.collection_run)
        self.assertTrue(
            EvidenceArtifactLink.objects.filter(
                evidence_item=evidence_item,
                raw_artifact=raw_artifact,
            ).exists()
        )
        self.assertEqual(evidence_item.data_domain, "EXTERNAL_DEMAND")

        # The deterministic offline AI provider only proposes. A human owner
        # accepts the exact evidence set before an Opportunity exists.
        proposal = propose_daily_analysis(
            batch_key=batch_key,
            product=self.product,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        self.assertEqual(proposal.method, "AI_PROPOSAL")
        self.assertEqual(proposal.decision_state, "PROPOSED")
        self.assertEqual(proposal.value["evidence_ids"], [str(evidence_item.pk)])
        self.assertFalse(ProductOpportunity.objects.exists())
        opportunity = accept_daily_analysis(
            proposal=proposal,
            product=self.product,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        self.assertEqual(
            SignalAssessment.objects.get(supersedes=proposal).decision_state,
            "APPROVED",
        )

        opportunity = self._transition_intelligence(
            transition_opportunity,
            aggregate=opportunity,
            to_state=ProductOpportunity.State.TRIAGED,
            expected_version=0,
            reason="Human triage accepted the exact external evidence.",
        )
        opportunity = self._transition_intelligence(
            transition_opportunity,
            aggregate=opportunity,
            to_state=ProductOpportunity.State.APPROVED,
            expected_version=1,
            reason="Owner approved planning from the bounded proposal.",
        )
        initiative = create_initiative_from_opportunity(
            opportunity=opportunity,
            command_id=uuid.uuid4(),
            principal=self.owner,
            acting_role=self.owner.role,
        )
        initiative = self._transition_intelligence(
            transition_initiative,
            aggregate=initiative,
            to_state=Initiative.State.APPROVED,
            expected_version=0,
            reason="Owner approved the initiative.",
        )
        plan = ChannelPlan.objects.create(
            initiative=initiative,
            channel_account=self.account,
            plan_key=f"{initiative.initiative_key}-tiktok",
            platform_code=Platform.TIKTOK.value,
            plan_date=timezone.localdate(),
            goal={"title": "Answer the afternoon-focus demand signal"},
            content_requirements={
                "task_title": "Create one link-only TikTok deliverable",
                "task_description": "Draft externally, save only a stable version link, and request review.",
                "environment_code": "legacy-wrong-environment",
                "capability_code": "LEGACY_WRONG_CAPABILITY",
            },
            creation_command_id=uuid.uuid4(),
            creation_payload_hash="1" * 64,
            created_by_principal=self.owner,
            created_under_grant=self.owner_grants[PermissionGrant.Action.EDIT],
            updated_by_principal=self.owner,
        )
        # Simulate a plan created by the earlier UI, where a user could type
        # arbitrary runtime codes.  Compilation must ignore those legacy
        # strings, resolve the one exact current binding fail-closed, and keep
        # the historical plan payload untouched for audit.
        plan = self._transition_intelligence(
            transition_channel_plan,
            aggregate=plan,
            to_state=ChannelPlan.State.READY,
            expected_version=0,
            reason="Exact account, environment and manual capability are ready.",
        )
        compiled = compile_channel_plan_task(
            channel_plan=plan,
            task_id=uuid.uuid4(),
            command_id=uuid.uuid4(),
            principal=self.owner,
            acting_role=self.owner.role,
        )
        task = compiled.task
        context = TaskCompilationContext.objects.get(task=task)
        self.assertEqual(context.channel_plan_id, plan.pk)
        self.assertEqual(context.product_profile_version_id, self.profile.pk)
        self.assertEqual(context.capability_state_id, self.capability.pk)
        plan.refresh_from_db()
        self.assertEqual(
            plan.content_requirements["environment_code"],
            "legacy-wrong-environment",
        )
        self.assertEqual(
            plan.content_requirements["capability_code"],
            "LEGACY_WRONG_CAPABILITY",
        )

        # Execute the compiled Task with an explicit assignee and a link-only
        # immutable ContentAssetVersion; no media bytes are uploaded.
        TaskCheckRun.record_completed(
            task=task,
            check_kind=TaskCheckRun.Kind.DOR,
            results=[
                {
                    "criterion_key": "source_ready",
                    "result": TaskCheckRun.Result.PASS,
                    "evidence": {"external_evidence_item_id": str(evidence_item.pk)},
                }
            ],
            command_id=uuid.uuid4(),
            evaluator_principal=self.owner,
            acting_role=self.owner.role,
            permission_grant=self.owner_grants[PermissionGrant.Action.EDIT],
            recorded_by_principal=self.owner,
        )
        self._transition_task(
            task,
            Task.State.READY,
            actor=self.owner,
            grant=self.owner_grants[PermissionGrant.Action.EDIT],
            reason="DoR passed.",
        )
        TaskAssignment.record(
            task=task,
            assignee_principal=self.operator,
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            assigned_by_principal=self.owner,
            acting_role=self.owner.role,
            permission_grant=self.owner_grants[PermissionGrant.Action.ASSIGN_TASK],
            recorded_by_principal=self.owner,
        )
        self._transition_task(
            task,
            Task.State.ASSIGNED,
            actor=self.owner,
            grant=self.owner_grants[PermissionGrant.Action.ASSIGN_TASK],
            reason="Assigned to the exact Operator.",
        )
        self._transition_task(
            task,
            Task.State.IN_PROGRESS,
            actor=self.operator,
            grant=self.operator_edit,
            reason="Operator started work.",
        )
        external_draft_url = "https://docs.example.test/puko/daily-v1"
        asset = ContentAsset.create_idempotent(
            task=task,
            asset_key="primary-link",
            title="Daily TikTok deliverable",
            asset_kind=ContentAsset.AssetKind.VIDEO,
            description="Link-only external draft; Growth OS stores no media file.",
            command_id=uuid.uuid4(),
            actor_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        asset_version = ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=external_draft_url,
            mime_type="text/uri-list",
            byte_size=len(external_draft_url.encode("utf-8")),
            content_sha256=hashlib.sha256(external_draft_url.encode("utf-8")).hexdigest(),
            metadata={
                "storage_mode": "LINK_ONLY",
                "provider": "external-docs",
                "version_reference": "daily-v1",
            },
            command_id=uuid.uuid4(),
            actor_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        dod = TaskCheckRun.record_completed(
            task=task,
            check_kind=TaskCheckRun.Kind.DOD,
            results=[
                {
                    "criterion_key": "external_link",
                    "result": TaskCheckRun.Result.PASS,
                    "evidence": {"url": external_draft_url},
                }
            ],
            command_id=uuid.uuid4(),
            evaluator_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        submission = TaskSubmission.seal(
            task=task,
            dod_check_run=dod,
            primary_asset_version=asset_version,
            submission_note="Stable external version link for human review.",
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            actor_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        self._transition_task(
            task,
            Task.State.SUBMITTED,
            actor=self.operator,
            grant=self.operator_edit,
            reason="Exact link-only version submitted.",
        )
        self._transition_task(
            task,
            Task.State.UNDER_REVIEW,
            actor=self.operator,
            grant=self.operator_edit,
            reason="Submission sent to a different human reviewer.",
        )

        # Negative: even an Operator with a live REVIEW grant cannot review
        # the same submission they authored.
        with self.assertRaises(ValidationError):
            ReviewDecision.record_final(
                submission=submission,
                decision=ReviewDecision.Decision.APPROVED,
                rationale="Self-review must be rejected.",
                command_id=uuid.uuid4(),
                expected_task_version=task.state_version,
                reviewer_principal=self.operator,
                acting_role=self.operator.role,
                permission_grant=self.operator_review,
                recorded_by_principal=self.operator,
            )
        self.assertFalse(ReviewDecision.objects.exists())
        review = ReviewDecision.record_final(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="A different human reviewer approved the exact linked version.",
            criteria_results={"external_link": "PASS"},
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            reviewer_principal=self.reviewer,
            acting_role=self.reviewer.role,
            permission_grant=self.reviewer_grant,
            recorded_by_principal=self.reviewer,
        )
        self.assertNotEqual(review.reviewer_principal_id, submission.submitted_by_principal_id)
        self._transition_task(
            task,
            Task.State.APPROVED,
            actor=self.owner,
            grant=self.owner_grants[PermissionGrant.Action.EDIT],
            reason="Exact final human review accepted.",
        )

        gate_result = orchestrate_v1_release_gate(
            task=task,
            submission=submission,
            publisher_principal=self.publisher,
            channel_account=self.account,
            runtime_environment=self.environment,
            command_id=uuid.uuid4(),
        )
        self.assertEqual(gate_result.gate.outcome, ReleaseGateRecord.Outcome.PASSED)
        publication = gate_result.publication
        self.assertEqual(publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)

        # Negative: a gate alone is not consent to publish. An unchecked human
        # confirmation creates no publication proof.
        events_before_rejection = publication.events.count()
        with self.assertRaises(ValidationError):
            prepare_human_publication_confirmation(
                publication=publication,
                publisher_principal=self.publisher,
                mode=PublicationMode.MANUAL,
                confirmation_id=uuid.uuid4(),
                confirmed=False,
            )
        self.assertEqual(publication.events.count(), events_before_rejection)

        confirmation = prepare_human_publication_confirmation(
            publication=publication,
            publisher_principal=self.publisher,
            mode=PublicationMode.MANUAL,
            confirmation_id=uuid.uuid4(),
            confirmed=True,
        )
        published = dispatch_confirmed_publication(
            publication=publication,
            publisher_principal=self.publisher,
            confirmation=confirmation,
            command_id=uuid.uuid4(),
            manual_external_url="https://www.tiktok.com/@puko/video/manual-daily-v1",
            manual_external_publication_id="manual-daily-v1",
        )
        self.assertEqual(published.status, PublicationDispatchStatus.SUCCEEDED)
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.MANUAL_PUBLISHED_RECORDED)
        self.assertEqual(
            published.publication_event.event_type,
            PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED,
        )
        self.assertEqual(
            published.publication_event.external_url,
            "https://www.tiktok.com/@puko/video/manual-daily-v1",
        )
        self._transition_task(
            task,
            Task.State.DONE,
            actor=self.owner,
            grant=self.owner_grants[PermissionGrant.Action.COMPLETE_TASK],
            reason="Manual publication proof recorded.",
        )

        # The performance fact binds the exact immutable Publication, rather
        # than only the channel account.
        performance = record_performance_rows(
            actor=self.owner,
            channel_account=self.account,
            publication=publication,
            rows=[
                {
                    "metric_key": "vertical-v1-views",
                    "metric_name": "Views",
                    "availability_state": AvailabilityState.PRESENT,
                    "numeric_value": Decimal("125"),
                    "unit": "count",
                    "observed_at": self.now + timedelta(hours=1),
                    "source_reference": "offline manual performance fixture",
                }
            ],
            source_kind="MANUAL",
            operation_key=str(uuid.uuid4()),
        )[0]
        self.assertIsInstance(performance, PublicationPerformanceObservation)
        self.assertEqual(performance.publication_id, publication.pk)
        self.assertEqual(performance.data_domain, DataDomain.CONTENT_PERFORMANCE)

        panel, _ = create_geo_panel_version(
            actor=self.owner,
            product=self.product,
            panel_key="vertical-daily-v1-geo",
            version_number=1,
            market_code="US",
            language_code="en",
        )
        panel_item, _ = add_geo_panel_item(
            actor=self.owner,
            panel=panel,
            item_number=1,
            question="What products support an afternoon focus routine?",
            intent="product discovery",
        )
        geo_result = record_geo_result(
            actor=self.owner,
            panel_item=panel_item,
            provider="offline-fake-geo",
            model_reference="offline-fixture-v1",
            availability_state=AvailabilityState.PRESENT,
            response_text="PUKO appeared in this bounded offline GEO fixture.",
            brand_mentioned=True,
            rank_position=1,
            citation_urls=["https://example.com/offline-geo-source"],
            operation_key=str(uuid.uuid4()),
        )
        geo_metric = GEOMetricObservation.objects.get(probe_result=geo_result)
        self.assertEqual(geo_metric.data_domain, DataDomain.GEO)
        learning = propose_learning(
            actor=self.owner,
            product=self.product,
            learning_key="vertical-geo-mention",
            title="Bounded GEO response mentioned PUKO",
            conclusion="One offline GEO observation mentioned the brand.",
            recommended_action="Propose a bounded rule clarification for human governance.",
            confidence=Decimal("0.7000"),
            evidence_ref=f"geo:{geo_metric.pk}",
            evidence_note="Exact GEO metric observation from the vertical fixture.",
        )
        self.assertEqual(learning.status, LearningVersion.Status.PROPOSED)
        learning_link = LearningEvidenceLink.objects.get(learning_version=learning)
        self.assertEqual(learning_link.geo_metric_id, geo_metric.pk)

        issue = create_issue(
            actor=self.owner,
            issue_key="vertical-daily-v1-learning-review",
            issue_type=Issue.IssueType.OPERATIONAL,
            severity=Issue.Severity.MEDIUM,
            title="Review the proposed GEO learning",
            description="The learning is evidence-linked but must not change policy automatically.",
        )
        meeting = create_meeting(
            actor=self.owner,
            participants=[self.owner, self.reviewer],
            meeting_key="vertical-daily-v1-governance",
            meeting_type=Meeting.MeetingType.RULE_GOVERNANCE,
            title="Daily Operations learning review",
            summary="Keep the learning proposed and create a human-governed candidate only.",
            occurred_at=self.now + timedelta(hours=2),
        )
        decision = create_meeting_decision(
            actor=self.owner,
            meeting=meeting,
            issue=issue,
            linkage_role="PRIMARY",
            decision_key="vertical-proposal-only",
            decision_type=MeetingDecision.DecisionType.RULE_PROPOSAL,
            decision="Create a proposal from the exact LearningVersion; do not activate it.",
            impact_scope={"market": "US", "product_id": str(self.product.pk)},
            owner_principal=self.owner,
        )
        candidate_policy = PolicyVersion.objects.create(
            policy_definition=self.policy_definition,
            version_number=2,
            rules=[
                {"rule_code": "exact_release_context", "required": True},
                {"rule_code": "human_review_geo_learning", "required": True},
            ],
            effective_from=self.now + timedelta(days=1),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        rule_proposal = create_rule_proposal(
            actor=self.owner,
            source_kind=RuleProposalSourceLink.SourceKind.LEARNING,
            source=learning,
            proposal_key="vertical-daily-v1-geo-clarification",
            target_policy_definition=self.policy_definition,
            candidate_policy_version=candidate_policy,
            change_effect=RuleProposalVersion.ChangeEffect.CLARIFY,
            risk_level=RuleProposalVersion.RiskLevel.MEDIUM,
            affected_scope={"market": "US", "product_id": str(self.product.pk)},
            rationale="Human-governed proposal based on the exact proposed learning.",
        )
        self.assertEqual(decision.issue_links.get().issue_id, issue.pk)
        self.assertEqual(rule_proposal.source_links.get().learning_version_id, learning.pk)
        self.assertEqual(learning.status, LearningVersion.Status.PROPOSED)
        self.assertFalse(PolicyActivation.objects.exists())
