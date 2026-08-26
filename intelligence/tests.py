from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from intelligence.exceptions import CommandReplayConflict, IllegalStateTransition, StateVersionConflict
from intelligence.models import (
    AssessmentMethod,
    AvailabilityState,
    ChannelPlan,
    CollectionRun,
    DataDomain,
    DemandAssessment,
    DemandEvidenceLink,
    EvidenceArtifactLink,
    ExternalEvidenceItem,
    Initiative,
    ProductOpportunity,
    ProductTopicFit,
    ProductTopicFitAssessment,
    RawArtifact,
    RiskLevel,
    SignalAssessment,
    SourceRegistry,
    TaskCompilationContext,
    Topic,
    canonical_sha256,
)
from intelligence.services import (
    record_collection_run,
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
    RuntimeEnvironment,
)
from workflow.models import Task, TaskContractPolicyLink, TaskContractVersion


class IntelligenceFoundationTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="intel-owner", password="local-test-password", role=Principal.Role.OWNER
        )
        self.other = Principal.objects.create_user(
            username="intel-other", password="local-test-password", role=Principal.Role.OPERATOR
        )
        self.product = Product.objects.create(
            product_code="PUKO-INTEL",
            name="PUKO Intelligence Test",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        now = timezone.now()
        self.edit_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=2),
            granted_by_principal=self.owner,
        )
        self.collect_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=2),
            granted_by_principal=self.owner,
        )
        self.create_task_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.CREATE_TASK,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=2),
            granted_by_principal=self.owner,
        )
        self.source = SourceRegistry.objects.create(
            source_key="tikhub-tiktok",
            version_number=1,
            platform_code="TIKTOK",
            source_kind=SourceRegistry.SourceKind.THIRD_PARTY_API,
            display_name="TikHub TikTok",
            non_secret_config={"route": "search"},
            created_by_principal=self.owner,
        )
        self.batch_key = uuid.uuid4()
        self.run = self._create_run(source=self.source, batch_key=self.batch_key)
        self.artifact = RawArtifact.objects.create(
            collection_run=self.run,
            source=self.source,
            external_url="https://www.tiktok.com/@example/video/1",
            external_content_id="video-1",
            observed_at=now,
            payload={"caption": "People asking how to focus"},
            content_sha256="",
            dedupe_key="artifact-video-1",
        )
        self.evidence = ExternalEvidenceItem.objects.create(
            source=self.source,
            collection_run=self.run,
            platform_code="TIKTOK",
            market_code="US",
            language_code="en",
            external_url="https://www.tiktok.com/@example/video/1",
            external_content_id="video-1",
            title="Focus question",
            excerpt="How can I focus in the afternoon?",
            facts={"question": "focus"},
            observed_at=now,
            provenance_sha256="",
            dedupe_key="evidence-video-1",
            created_by_principal=self.owner,
        )
        self.topic = Topic.objects.create(
            topic_key="afternoon-focus",
            version_number=1,
            market_code="US",
            language_code="en",
            label="Afternoon focus",
            summary="People want help sustaining afternoon focus.",
            pain_points=["afternoon slump"],
            created_by_principal=self.owner,
        )
        self.demand = DemandAssessment.objects.create(
            topic=self.topic,
            version_number=1,
            window_start=now - timedelta(days=7),
            window_end=now,
            demand_score=Decimal("0.8000"),
            velocity_score=Decimal("0.2000"),
            confidence=Decimal("0.7500"),
            availability_state=AvailabilityState.PRESENT,
            method=AssessmentMethod.DETERMINISTIC,
            assessed_by_principal=self.owner,
        )
        self.fit = ProductTopicFit.objects.create(
            product=self.product, topic=self.topic, created_by_principal=self.owner
        )
        self.fit_assessment = ProductTopicFitAssessment.objects.create(
            product_topic_fit=self.fit,
            version_number=1,
            fit_score=Decimal("0.9000"),
            evidence_strength=Decimal("0.7000"),
            method=AssessmentMethod.HUMAN,
            rationale="The product and audience facts match this topic.",
            assessed_by_principal=self.owner,
        )
        self.opportunity = ProductOpportunity.objects.create(
            product=self.product,
            topic=self.topic,
            demand_assessment=self.demand,
            product_topic_fit_assessment=self.fit_assessment,
            opportunity_key="puko-afternoon-focus",
            title="Answer afternoon focus questions",
            recommendation="Create an evidence-linked answer.",
            priority_score=Decimal("0.8200"),
            risk_level=RiskLevel.MEDIUM,
            creation_command_id=uuid.uuid4(),
            creation_payload_hash=canonical_sha256({"opportunity": "puko-afternoon-focus"}),
            created_by_principal=self.owner,
            created_under_grant=self.edit_grant,
            updated_by_principal=self.owner,
        )

    def _create_run(self, *, source, batch_key, attempt_number=1):
        now = timezone.now()
        query_spec = {"query": "focus", "limit": 10}
        operation_key = uuid.uuid4()
        return CollectionRun.objects.create(
            source=source,
            batch_key=batch_key,
            attempt_number=attempt_number,
            operation_key=operation_key,
            request_payload_hash=canonical_sha256(query_spec),
            operation_payload_hash=canonical_sha256({"operation_key": str(operation_key)}),
            query_spec=query_spec,
            status=CollectionRun.Status.SUCCEEDED,
            availability_state=AvailabilityState.PRESENT,
            started_at=now - timedelta(seconds=1),
            completed_at=now,
            result_summary={"items": 1},
            executed_by_principal=self.owner,
            permission_grant=self.collect_grant,
        )

    def _create_initiative(self):
        self.opportunity.refresh_from_db()
        if self.opportunity.current_state == ProductOpportunity.State.PROPOSED:
            transition_opportunity(
                opportunity_id=self.opportunity.pk,
                to_state=ProductOpportunity.State.TRIAGED,
                expected_version=0,
                command_id=uuid.uuid4(),
                reason="Human triage accepted the evidence package.",
                principal=self.owner,
                acting_role=Principal.Role.OWNER,
            )
            transition_opportunity(
                opportunity_id=self.opportunity.pk,
                to_state=ProductOpportunity.State.APPROVED,
                expected_version=1,
                command_id=uuid.uuid4(),
                reason="Owner approved planning from this Opportunity.",
                principal=self.owner,
                acting_role=Principal.Role.OWNER,
            )
            self.opportunity.refresh_from_db()
        return Initiative.objects.create(
            product=self.product,
            opportunity=self.opportunity,
            initiative_key=f"daily-{uuid.uuid4()}",
            title="Daily focus initiative",
            objective="Turn approved external demand into one daily task.",
            creation_command_id=uuid.uuid4(),
            creation_payload_hash=canonical_sha256({"initiative": str(uuid.uuid4())}),
            created_by_principal=self.owner,
            created_under_grant=self.edit_grant,
            updated_by_principal=self.owner,
        )

    def test_link_only_provenance_and_same_run_artifact_link(self):
        self.assertEqual(self.artifact.content_sha256, canonical_sha256(self.artifact.payload))
        self.assertEqual(self.evidence.provenance_sha256, canonical_sha256(self.evidence.provenance_payload()))
        EvidenceArtifactLink.objects.create(
            evidence_item=self.evidence, raw_artifact=self.artifact, created_by_principal=self.owner
        )

        field_names = {field.name for field in RawArtifact._meta.fields}
        self.assertFalse({"file", "upload", "blob", "media_file"} & field_names)

        other_run = self._create_run(source=self.source, batch_key=uuid.uuid4())
        other_artifact = RawArtifact.objects.create(
            collection_run=other_run,
            source=self.source,
            external_url="https://example.com/other",
            observed_at=timezone.now(),
            payload={"text": "other"},
            content_sha256="",
            dedupe_key="artifact-other-run",
        )
        with self.assertRaises(ValidationError):
            EvidenceArtifactLink.objects.create(
                evidence_item=self.evidence,
                raw_artifact=other_artifact,
                created_by_principal=self.owner,
            )

    def test_collection_batch_attempts_are_durable_and_unique_per_source(self):
        retry = self._create_run(source=self.source, batch_key=self.batch_key, attempt_number=2)
        self.assertEqual(retry.batch_key, self.batch_key)
        with self.assertRaises(ValidationError):
            self._create_run(source=self.source, batch_key=self.batch_key, attempt_number=2)

        csv_source = SourceRegistry.objects.create(
            source_key="tiktok-csv",
            version_number=1,
            platform_code="TIKTOK",
            source_kind=SourceRegistry.SourceKind.CSV,
            display_name="TikTok CSV fallback",
            created_by_principal=self.owner,
        )
        parallel_source_run = self._create_run(source=csv_source, batch_key=self.batch_key, attempt_number=1)
        self.assertEqual(parallel_source_run.batch_key, self.batch_key)

        now = timezone.now()
        operation_key = uuid.uuid4()
        recorded = record_collection_run(
            source=self.source,
            batch_key=self.batch_key,
            attempt_number=3,
            operation_key=operation_key,
            query_spec={"query": "focus", "limit": 3},
            status=CollectionRun.Status.PARTIAL,
            availability_state=AvailabilityState.PRESENT,
            started_at=now - timedelta(seconds=2),
            completed_at=now,
            result_summary={"items": 2},
            error_code="RATE_LIMITED_AFTER_PARTIAL",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        replay = record_collection_run(
            source=self.source,
            batch_key=self.batch_key,
            attempt_number=3,
            operation_key=operation_key,
            query_spec={"query": "focus", "limit": 3},
            status=CollectionRun.Status.PARTIAL,
            availability_state=AvailabilityState.PRESENT,
            started_at=now - timedelta(seconds=2),
            completed_at=now,
            result_summary={"items": 2},
            error_code="RATE_LIMITED_AFTER_PARTIAL",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        self.assertTrue(recorded.created)
        self.assertFalse(replay.created)
        self.assertEqual(recorded.run.pk, replay.run.pk)
        with self.assertRaises(CommandReplayConflict):
            record_collection_run(
                source=self.source,
                batch_key=self.batch_key,
                attempt_number=3,
                operation_key=operation_key,
                query_spec={"query": "changed"},
                status=CollectionRun.Status.FAILED,
                availability_state=AvailabilityState.BLOCKED,
                started_at=now - timedelta(seconds=2),
                completed_at=now,
                result_summary={},
                error_code="CHANGED",
                principal=self.owner,
                acting_role=Principal.Role.OWNER,
            )

    def test_collection_write_guard_preserves_exact_replay_after_human_decision(self):
        now = timezone.now()
        batch_key = uuid.uuid4()
        operation_key = uuid.uuid4()
        call = {
            "source": self.source,
            "batch_key": batch_key,
            "attempt_number": 1,
            "operation_key": operation_key,
            "query_spec": {"product_id": str(self.product.pk), "query": "focus"},
            "status": CollectionRun.Status.SUCCEEDED,
            "availability_state": AvailabilityState.PRESENT,
            "started_at": now - timedelta(seconds=2),
            "completed_at": now,
            "result_summary": {"items": 1},
            "error_code": "",
            "principal": self.owner,
            "acting_role": Principal.Role.OWNER,
        }
        recorded = record_collection_run(**call)
        evidence = ExternalEvidenceItem.objects.create(
            source=self.source,
            collection_run=recorded.run,
            platform_code="TIKTOK",
            market_code="US",
            language_code="en",
            external_url="https://www.tiktok.com/@example/video/human-decision",
            external_content_id="human-decision",
            title="Human decision evidence",
            excerpt="Exact evidence used by the human decision.",
            facts={"query": "focus"},
            observed_at=now,
            provenance_sha256="",
            dedupe_key=f"human-decision-{batch_key.hex}",
            created_by_principal=self.owner,
        )
        SignalAssessment.objects.create(
            evidence_item=evidence,
            assessment_key=f"daily-analysis-{batch_key.hex}",
            version_number=1,
            signal_type="DAILY_OPERATIONS_ANALYSIS",
            value={"product_id": str(self.product.pk), "batch_key": str(batch_key)},
            confidence=Decimal("0.8000"),
            method=AssessmentMethod.HUMAN,
            decision_state="APPROVED",
            rationale="A human accepted this exact evidence set.",
            assessed_by_principal=self.owner,
            decided_by_principal=self.owner,
        )

        replay = record_collection_run(**call)
        self.assertFalse(replay.created)
        self.assertEqual(replay.run.pk, recorded.run.pk)

        blocked_call = dict(call)
        blocked_call.update(operation_key=uuid.uuid4(), attempt_number=2)
        with self.assertRaisesMessage(ValidationError, "这次建议已经由人工采用"):
            record_collection_run(**blocked_call)
        self.assertEqual(CollectionRun.objects.filter(batch_key=batch_key).count(), 1)

    def test_collection_write_guard_rejects_missing_or_forged_batch_product(self):
        now = timezone.now()
        batch_key = uuid.uuid4()
        first_call = {
            "source": self.source,
            "batch_key": batch_key,
            "attempt_number": 1,
            "operation_key": uuid.uuid4(),
            "query_spec": {"product_id": str(self.product.pk), "query": "focus"},
            "status": CollectionRun.Status.SUCCEEDED,
            "availability_state": AvailabilityState.PRESENT,
            "started_at": now - timedelta(seconds=2),
            "completed_at": now,
            "result_summary": {"items": 1},
            "error_code": "",
            "principal": self.owner,
            "acting_role": Principal.Role.OWNER,
        }
        first = record_collection_run(**first_call)

        missing_product_call = dict(first_call)
        missing_product_call.update(
            attempt_number=2,
            operation_key=uuid.uuid4(),
            query_spec={"query": "focus without product"},
        )
        with self.assertRaisesMessage(ValidationError, "exact Product identity"):
            record_collection_run(**missing_product_call)

        forged_product_call = dict(first_call)
        forged_product_call.update(
            attempt_number=2,
            operation_key=uuid.uuid4(),
            query_spec={"product_id": str(uuid.uuid4()), "query": "focus for another product"},
        )
        with self.assertRaisesMessage(ValidationError, "exact Product identity"):
            record_collection_run(**forged_product_call)

        self.assertEqual(CollectionRun.objects.filter(batch_key=batch_key).count(), 1)
        self.assertEqual(first.run.query_spec["product_id"], str(self.product.pk))

    def test_collection_write_guard_keeps_first_generic_batch_generic(self):
        now = timezone.now()
        batch_key = uuid.uuid4()
        first = record_collection_run(
            source=self.source,
            batch_key=batch_key,
            attempt_number=1,
            operation_key=uuid.uuid4(),
            query_spec={"query": "generic research"},
            status=CollectionRun.Status.SUCCEEDED,
            availability_state=AvailabilityState.PRESENT,
            started_at=now - timedelta(seconds=2),
            completed_at=now,
            result_summary={"items": 1},
            error_code="",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        second = record_collection_run(
            source=self.source,
            batch_key=batch_key,
            attempt_number=2,
            operation_key=uuid.uuid4(),
            query_spec={"query": "generic research retry"},
            status=CollectionRun.Status.PARTIAL,
            availability_state=AvailabilityState.PRESENT,
            started_at=now,
            completed_at=now,
            result_summary={"items": 0},
            error_code="NO_NEW_ITEMS",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )

        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertNotIn("product_id", first.run.query_spec)
        self.assertNotIn("product_id", second.run.query_spec)

    def test_collection_write_guard_rejects_new_write_after_product_opportunity(self):
        now = timezone.now()
        batch_key = uuid.uuid4()
        first = record_collection_run(
            source=self.source,
            batch_key=batch_key,
            attempt_number=1,
            operation_key=uuid.uuid4(),
            query_spec={"product_id": str(self.product.pk), "query": "focus"},
            status=CollectionRun.Status.SUCCEEDED,
            availability_state=AvailabilityState.PRESENT,
            started_at=now - timedelta(seconds=2),
            completed_at=now,
            result_summary={"items": 1},
            error_code="",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        ProductOpportunity.objects.create(
            product=self.product,
            topic=self.topic,
            demand_assessment=self.demand,
            product_topic_fit_assessment=self.fit_assessment,
            opportunity_key=f"daily-{batch_key.hex}",
            title="Human-approved daily opportunity",
            recommendation="Keep the accepted evidence set frozen.",
            priority_score=Decimal("0.8000"),
            risk_level=RiskLevel.MEDIUM,
            creation_command_id=uuid.uuid4(),
            creation_payload_hash=canonical_sha256({"batch_key": str(batch_key)}),
            created_by_principal=self.owner,
            created_under_grant=self.edit_grant,
            updated_by_principal=self.owner,
        )

        with self.assertRaisesMessage(ValidationError, "这次建议已经由人工采用"):
            record_collection_run(
                source=self.source,
                batch_key=batch_key,
                attempt_number=2,
                operation_key=uuid.uuid4(),
                query_spec={"product_id": str(self.product.pk), "query": "focus again"},
                status=CollectionRun.Status.PARTIAL,
                availability_state=AvailabilityState.PRESENT,
                started_at=now,
                completed_at=now,
                result_summary={"items": 0},
                error_code="NO_NEW_ITEMS",
                principal=self.owner,
                acting_role=Principal.Role.OWNER,
            )
        self.assertEqual(CollectionRun.objects.filter(batch_key=batch_key).count(), 1)
        self.assertEqual(first.run.batch_key, batch_key)

    def test_external_demand_rejects_cross_domain_mutation_and_facts_are_immutable(self):
        self.evidence.data_domain = "GEO"
        with self.assertRaises(ValidationError):
            self.evidence.save()
        with self.assertRaises(ValidationError):
            ExternalEvidenceItem.objects.filter(pk=self.evidence.pk).update(title="rewritten")

        DemandEvidenceLink.objects.create(
            demand_assessment=self.demand,
            evidence_item=ExternalEvidenceItem.objects.get(pk=self.evidence.pk),
            weight=Decimal("1.0000"),
            created_by_principal=self.owner,
        )

    def test_opportunity_state_transition_is_locked_idempotent_and_audited(self):
        command_id = uuid.uuid4()
        first = transition_opportunity(
            opportunity_id=self.opportunity.pk,
            to_state=ProductOpportunity.State.TRIAGED,
            expected_version=0,
            command_id=command_id,
            reason="Evidence and product fit are ready for human triage.",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        replay = transition_opportunity(
            opportunity_id=self.opportunity.pk,
            to_state=ProductOpportunity.State.TRIAGED,
            expected_version=0,
            command_id=command_id,
            reason="Evidence and product fit are ready for human triage.",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(first.event.pk, replay.event.pk)
        self.assertEqual(first.event.permission_grant_id, self.edit_grant.pk)
        self.assertEqual(first.aggregate.state_version, 1)

        with self.assertRaises(CommandReplayConflict):
            transition_opportunity(
                opportunity_id=self.opportunity.pk,
                to_state=ProductOpportunity.State.REJECTED,
                expected_version=0,
                command_id=command_id,
                reason="different payload",
                principal=self.owner,
                acting_role=Principal.Role.OWNER,
            )
        with self.assertRaises(StateVersionConflict):
            transition_opportunity(
                opportunity_id=self.opportunity.pk,
                to_state=ProductOpportunity.State.APPROVED,
                expected_version=0,
                command_id=uuid.uuid4(),
                reason="stale writer",
                principal=self.owner,
                acting_role=Principal.Role.OWNER,
            )

        refreshed = ProductOpportunity.objects.get(pk=self.opportunity.pk)
        refreshed.current_state = ProductOpportunity.State.APPROVED
        with self.assertRaises(ValidationError):
            refreshed.save()
        with self.assertRaises(ValidationError):
            ProductOpportunity.objects.filter(pk=self.opportunity.pk).update(
                current_state=ProductOpportunity.State.APPROVED
            )

    def test_illegal_and_unauthorized_transitions_fail_closed(self):
        with self.assertRaises(IllegalStateTransition):
            transition_opportunity(
                opportunity_id=self.opportunity.pk,
                to_state=ProductOpportunity.State.PLANNED,
                expected_version=0,
                command_id=uuid.uuid4(),
                reason="skip human triage",
                principal=self.owner,
                acting_role=Principal.Role.OWNER,
            )
        with self.assertRaises(PermissionDenied):
            transition_opportunity(
                opportunity_id=self.opportunity.pk,
                to_state=ProductOpportunity.State.TRIAGED,
                expected_version=0,
                command_id=uuid.uuid4(),
                reason="operator has no edit grant",
                principal=self.other,
                acting_role=Principal.Role.OPERATOR,
            )
        self.opportunity.refresh_from_db()
        self.assertEqual(self.opportunity.current_state, ProductOpportunity.State.PROPOSED)
        self.assertEqual(self.opportunity.state_events.count(), 0)

    def test_initiative_and_channel_plan_use_the_same_event_projection_rules(self):
        initiative = self._create_initiative()
        approved = transition_initiative(
            initiative_id=initiative.pk,
            to_state=Initiative.State.APPROVED,
            expected_version=0,
            command_id=uuid.uuid4(),
            reason="Owner approved daily planning.",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        self.assertTrue(approved.created)
        initiative.refresh_from_db()
        plan = ChannelPlan.objects.create(
            initiative=initiative,
            plan_key=f"tiktok-{uuid.uuid4()}",
            platform_code="TIKTOK",
            plan_date=timezone.localdate(),
            goal={"primary": "reach"},
            content_requirements={"format": "short-form"},
            creation_command_id=uuid.uuid4(),
            creation_payload_hash=canonical_sha256({"platform": "TIKTOK"}),
            created_by_principal=self.owner,
            created_under_grant=self.edit_grant,
            updated_by_principal=self.owner,
        )
        ready = transition_channel_plan(
            channel_plan_id=plan.pk,
            to_state=ChannelPlan.State.READY,
            expected_version=0,
            command_id=uuid.uuid4(),
            reason="Channel requirements are complete.",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        self.assertEqual(ready.aggregate.current_state, ChannelPlan.State.READY)
        self.assertEqual(ready.event.permission_grant_id, self.edit_grant.pk)

    def test_task_compilation_context_binds_typed_sealed_versions_and_exact_hashes(self):
        objective = ObjectiveProfileVersion.objects.create(
            objective_key="DAILY_REACH",
            version_number=1,
            primary_objectives=["reach"],
            secondary_objectives=["engagement"],
            retained_metrics=["purchase"],
            priority_rules={"reach": 1},
            strategy_boundaries={"human_approval": True},
            created_by_principal=self.owner,
        )
        objective.seal(principal=self.owner)
        claim_matrix = ClaimMatrixVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
        )
        claim_matrix.seal(principal=self.owner)
        library = EvidenceLibraryVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
        )
        library.seal(principal=self.owner)
        profile = ProductProfileVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"intent": "focus"},
            core_value_proposition="Evidence-informed focus support.",
            brand_voice={},
            product_facts={},
            prohibited_expressions=[],
            objective_profile_version=objective,
            claim_matrix_version=claim_matrix,
            evidence_library_version=library,
            created_by_principal=self.owner,
        )
        profile.seal(self.owner)
        contract = TaskContractVersion.objects.create(
            product_profile_version=profile,
            version_number=1,
            title="TikTok evidence task",
            dor_criteria=[{"key": "evidence_ready", "required": True}],
            dod_criteria=[{"key": "link_ready", "required": True}],
            release_gate_criteria=[{"key": "policy_pass", "required": True}],
            success_criteria=[{"key": "observation_planned", "required": True}],
            sealed_at=timezone.now(),
            created_by_principal=self.owner,
        )
        task = Task.objects.create(
            product=self.product,
            product_profile_version=profile,
            contract_version=contract,
            title="Compile one TikTok task",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        policy_definition = PolicyDefinition.objects.create(
            policy_code="INTEL-SAFE",
            name="Intelligence test policy",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        policy = PolicyVersion.objects.create(
            policy_definition=policy_definition,
            version_number=1,
            rules=[{"rule_code": "NO_UNSUPPORTED_CLAIMS", "required": True}],
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        TaskContractPolicyLink.objects.create(
            task_contract_version=contract,
            policy_version=policy,
            required=True,
            created_by_principal=self.owner,
        )
        account = ChannelAccount.objects.create(
            platform_code="TIKTOK",
            account_code="intel-tiktok",
            external_account_ref="@puko-test",
            display_name="PUKO test TikTok",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        environment = RuntimeEnvironment.objects.create(
            environment_code="intel-staging",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="intel-test",
            database_namespace="intel-test",
            object_storage_namespace="link-only",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        binding = AccountEnvironmentBinding.objects.create(
            channel_account=account,
            runtime_environment=environment,
            binding_version=1,
            identity_reference="test-identity",
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        capability = CapabilityState.objects.create(
            account_environment_binding=binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=1,
            state=CapabilityState.State.OPEN,
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        initiative = self._create_initiative()
        transition_initiative(
            initiative_id=initiative.pk,
            to_state=Initiative.State.APPROVED,
            expected_version=0,
            command_id=uuid.uuid4(),
            reason="Owner approved this daily initiative.",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        initiative.refresh_from_db()
        plan = ChannelPlan.objects.create(
            initiative=initiative,
            channel_account=account,
            plan_key=f"compile-{uuid.uuid4()}",
            platform_code="TIKTOK",
            plan_date=timezone.localdate(),
            goal={"primary": "reach"},
            content_requirements={"format": "short-form"},
            creation_command_id=uuid.uuid4(),
            creation_payload_hash=canonical_sha256({"compile": True}),
            created_by_principal=self.owner,
            created_under_grant=self.edit_grant,
            updated_by_principal=self.owner,
        )
        transition_channel_plan(
            channel_plan_id=plan.pk,
            to_state=ChannelPlan.State.READY,
            expected_version=0,
            command_id=uuid.uuid4(),
            reason="Exact task inputs are ready for compilation.",
            principal=self.owner,
            acting_role=Principal.Role.OWNER,
        )
        plan.refresh_from_db()
        policy_snapshot = [{"id": str(policy.pk), "manifest_sha256": policy.manifest_sha256}]
        context = TaskCompilationContext.objects.create(
            task=task,
            channel_plan=plan,
            product=self.product,
            product_profile_version=profile,
            task_contract_version=contract,
            objective_profile_version=objective,
            objective_profile_manifest_sha256=objective.manifest_sha256,
            claim_matrix_version=claim_matrix,
            claim_matrix_manifest_sha256=claim_matrix.manifest_sha256,
            evidence_library_version=library,
            evidence_library_manifest_sha256=library.manifest_sha256,
            policy_set_snapshot=policy_snapshot,
            policy_set_sha256="",
            capability_state=capability,
            compiler_name="daily-task-compiler",
            compiler_version="1.0.0",
            input_payload_sha256=canonical_sha256({"task": str(task.pk)}),
            compilation_command_id=uuid.uuid4(),
            compiled_by_principal=self.owner,
            permission_grant=self.create_task_grant,
        )
        self.assertEqual(context.objective_profile_version_id, objective.pk)
        self.assertEqual(context.claim_matrix_version_id, claim_matrix.pk)
        self.assertEqual(context.evidence_library_version_id, library.pk)
        self.assertEqual(context.policy_set_sha256, canonical_sha256(policy_snapshot))

        alternate_objective = ObjectiveProfileVersion.objects.create(
            objective_key="DAILY_REACH",
            version_number=2,
            primary_objectives=["seo"],
            secondary_objectives=["reach"],
            retained_metrics=["purchase"],
            priority_rules={"seo": 1},
            strategy_boundaries={"human_approval": True},
            created_by_principal=self.owner,
        )
        alternate_objective.seal(principal=self.owner)
        alternate_claims = ClaimMatrixVersion.objects.create(
            product=self.product,
            version_number=2,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
        )
        alternate_claims.seal(principal=self.owner)
        alternate_library = EvidenceLibraryVersion.objects.create(
            product=self.product,
            version_number=2,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
        )
        alternate_library.seal(principal=self.owner)
        for relation_name, hash_name, alternate, original in (
            ("objective_profile_version", "objective_profile_manifest_sha256", alternate_objective, objective),
            ("claim_matrix_version", "claim_matrix_manifest_sha256", alternate_claims, claim_matrix),
            ("evidence_library_version", "evidence_library_manifest_sha256", alternate_library, library),
        ):
            setattr(context, relation_name, alternate)
            setattr(context, hash_name, alternate.manifest_sha256)
            with self.assertRaises(ValidationError):
                context.clean()
            setattr(context, relation_name, original)
            setattr(context, hash_name, original.manifest_sha256)

        unrelated_policy_definition = PolicyDefinition.objects.create(
            policy_code="INTEL-UNRELATED",
            name="Unrelated policy",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        unrelated_policy = PolicyVersion.objects.create(
            policy_definition=unrelated_policy_definition,
            version_number=1,
            rules=[{"rule_code": "OTHER", "required": True}],
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        context.policy_set_snapshot = [
            {"id": str(unrelated_policy.pk), "manifest_sha256": unrelated_policy.manifest_sha256}
        ]
        context.policy_set_sha256 = canonical_sha256(context.policy_set_snapshot)
        with self.assertRaises(ValidationError):
            context.clean()
        context.policy_set_snapshot = policy_snapshot
        context.policy_set_sha256 = canonical_sha256(policy_snapshot)

        other_account = ChannelAccount.objects.create(
            platform_code="TIKTOK",
            account_code="intel-tiktok-other",
            external_account_ref="@puko-other",
            display_name="Other TikTok",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        other_binding = AccountEnvironmentBinding.objects.create(
            channel_account=other_account,
            runtime_environment=environment,
            binding_version=1,
            identity_reference="other-identity",
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        other_capability = CapabilityState.objects.create(
            account_environment_binding=other_binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=1,
            state=CapabilityState.State.OPEN,
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        context.capability_state = other_capability
        with self.assertRaises(ValidationError):
            context.clean()
        context.capability_state = capability

        with self.assertRaises(ValidationError):
            context.policy_set_snapshot = []
            context.save()
