from __future__ import annotations

import shutil
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import include, path, reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAsset, ContentAssetVersion, ReviewDecision, TaskSubmission
from dashboard.review_views import (
    release_detail,
    release_done_action,
    release_gate_action,
    release_proof_action,
    release_queue,
    review_action,
    review_detail,
    review_queue,
)
from products.models import Product, ProductProfileVersion
from releasegate.models import (
    AccountEnvironmentBinding,
    CapabilityState,
    ChannelAccount,
    PolicyDefinition,
    PolicyVersion,
    Publication,
    PublicationEvent,
    ReleaseGateRecord,
    RuleEvaluationRun,
    RuntimeEnvironment,
)
from workflow.models import (
    ActingRole,
    Task,
    TaskAssignment,
    TaskCheckRun,
    TaskContractPolicyLink,
    TaskContractVersion,
)


def empty_view(_request):
    return HttpResponse("ok")


dashboard_patterns = [
    path("", empty_view, name="home"),
    path("review/", review_queue, name="review-queue"),
    path("review/<uuid:task_id>/", review_detail, name="review-detail"),
    path("review/<uuid:task_id>/action/", review_action, name="review-action"),
    path("release/", release_queue, name="release-queue"),
    path("release/<uuid:task_id>/", release_detail, name="release-detail"),
    path("release/<uuid:task_id>/gate/", release_gate_action, name="release-gate-action"),
    path("release/<uuid:task_id>/proof/", release_proof_action, name="release-proof-action"),
    path("release/<uuid:task_id>/done/", release_done_action, name="release-done-action"),
]
urlpatterns = [
    path("", include((dashboard_patterns, "dashboard"), namespace="dashboard")),
    path("login/", empty_view, name="login"),
    path("logout/", empty_view, name="logout"),
]


@override_settings(ROOT_URLCONF=__name__)
class ReviewReleaseUISliceTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="growth-os-proof-tests-")
        self.media_override = override_settings(MEDIA_ROOT=self.media_root)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, True)
        now = timezone.now()
        self.owner = Principal.objects.create_user(
            username="ui-owner", password="test-only", role=Principal.Role.OWNER
        )
        self.operator = Principal.objects.create_user(
            username="ui-operator", password="test-only", role=Principal.Role.OPERATOR
        )
        self.reviewer = Principal.objects.create_user(
            username="ui-reviewer", password="test-only", role=Principal.Role.OPERATIONS_ADMIN
        )
        self.publisher = Principal.objects.create_user(
            username="ui-publisher", password="test-only", role=Principal.Role.OPERATOR
        )
        self.outsider = Principal.objects.create_user(
            username="ui-outsider", password="test-only", role=Principal.Role.OPERATOR
        )
        self.evaluator = Principal.objects.create_user(
            username="ui-service-evaluator",
            principal_type=Principal.PrincipalType.SERVICE_ACCOUNT,
            role=Principal.Role.OPERATOR,
        )
        self.product = Product.objects.create(
            product_code="UI-PUKO",
            name="PUKO UI Pilot",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.profile = ProductProfileVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"market": "US"},
            core_value_proposition="Evidence-informed wellness.",
            brand_voice={"tone": "clear"},
            product_facts={"mode": "B2C"},
            prohibited_expressions=["cure"],
            created_by_principal=self.owner,
        )
        self.profile.seal(self.owner)
        policy_definition = PolicyDefinition.objects.create(
            policy_code="UI_EXACT_CONTEXT",
            name="UI exact release context",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.policy = PolicyVersion.objects.create(
            policy_definition=policy_definition,
            version_number=1,
            rules=[{"rule_code": "exact_release_context", "required": True}],
            effective_from=now - timedelta(minutes=5),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.contract = TaskContractVersion.objects.create(
            product_profile_version=self.profile,
            version_number=1,
            title="UI manual release",
            dor_criteria=[{"key": "ready", "required": True}],
            dod_criteria=[{"key": "complete", "required": True}],
            release_gate_criteria=[{"key": "exact_release_context", "required": True}],
            success_criteria=[{"key": "proof", "required": True}],
            sealed_at=now,
            created_by_principal=self.owner,
        )
        TaskContractPolicyLink.objects.create(
            task_contract_version=self.contract,
            policy_version=self.policy,
            required=True,
            created_by_principal=self.owner,
        )
        self.owner_edit = self._grant(self.owner, PermissionGrant.Action.EDIT)
        self.operator_edit = self._grant(self.operator, PermissionGrant.Action.EDIT)
        self.reviewer_review = self._grant(self.reviewer, PermissionGrant.Action.REVIEW)
        self.reviewer_edit = self._grant(self.reviewer, PermissionGrant.Action.EDIT)
        self.evaluator_review = self._grant(self.evaluator, PermissionGrant.Action.REVIEW)
        self.channel = ChannelAccount.objects.create(
            platform_code="QUORA",
            account_code="puko-ui-business",
            external_account_ref="puko-ui-business",
            display_name="PUKO UI Business",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.publisher_grant = PermissionGrant.objects.create(
            principal=self.publisher,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref=self.channel.account_code,
            action=PermissionGrant.Action.PUBLISH,
            effect=PermissionGrant.Effect.ALLOW,
            valid_from=now - timedelta(minutes=5),
            valid_until=now + timedelta(hours=2),
            granted_by_principal=self.owner,
        )
        self.environment = RuntimeEnvironment.objects.create(
            environment_code="ui-staging",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="ui-identity",
            database_namespace="ui-database",
            object_storage_namespace="ui-storage",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.binding = AccountEnvironmentBinding.objects.create(
            channel_account=self.channel,
            runtime_environment=self.environment,
            binding_version=1,
            identity_reference="ui-session",
            valid_from=now - timedelta(minutes=5),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.capability = CapabilityState.objects.create(
            account_environment_binding=self.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=1,
            state=CapabilityState.State.OPEN,
            effective_from=now - timedelta(minutes=5),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.task = self._under_review_task()

    def _grant(self, principal, action):
        return PermissionGrant.objects.create(
            principal=principal,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=action,
            effect=PermissionGrant.Effect.ALLOW,
            valid_from=timezone.now() - timedelta(minutes=5),
            valid_until=timezone.now() + timedelta(hours=2),
            granted_by_principal=self.owner,
        )

    def _transition(self, task, target, principal=None, grant=None):
        principal = principal or self.owner
        grant = grant or self.owner_edit
        task.refresh_from_db()
        event = Task.transition(
            task_id=task.pk,
            to_state=target,
            command_id=uuid.uuid4(),
            expected_state_version=task.state_version,
            actor_principal=principal,
            acting_role=principal.role,
            permission_grant=grant,
            recorded_by_principal=principal,
        )
        task.refresh_from_db()
        return event

    def _under_review_task(self):
        task = Task.objects.create(
            product=self.product,
            product_profile_version=self.profile,
            contract_version=self.contract,
            title="Review and manually publish a PUKO answer",
            description="Make the content clear before any human publication.",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        TaskCheckRun.record_completed(
            task=task,
            check_kind=TaskCheckRun.Kind.DOR,
            results=[{"criterion_key": "ready", "result": "PASS", "evidence": {"ok": True}}],
            command_id=uuid.uuid4(),
            evaluator_principal=self.owner,
            acting_role=self.owner.role,
            permission_grant=self.owner_edit,
            recorded_by_principal=self.owner,
        )
        self._transition(task, Task.State.READY)
        TaskAssignment.record(
            task=task,
            assignee_principal=self.operator,
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            assigned_by_principal=self.owner,
            acting_role=self.owner.role,
            permission_grant=self.owner_edit,
            recorded_by_principal=self.owner,
        )
        task.refresh_from_db()
        self._transition(task, Task.State.ASSIGNED)
        self._transition(task, Task.State.IN_PROGRESS)
        asset = ContentAsset.create_idempotent(
            task=task,
            asset_key="primary-ui",
            title="UI deliverable",
            asset_kind=ContentAsset.AssetKind.COPY,
            command_id=uuid.uuid4(),
            actor_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        version = ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=f"ui/{task.pk}/answer.txt",
            mime_type="text/plain",
            byte_size=24,
            content_sha256="a" * 64,
            metadata={"original_filename": "answer.txt"},
            command_id=uuid.uuid4(),
            actor_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        dod = TaskCheckRun.record_completed(
            task=task,
            check_kind=TaskCheckRun.Kind.DOD,
            results=[{"criterion_key": "complete", "result": "PASS", "evidence": {"version": str(version.pk)}}],
            command_id=uuid.uuid4(),
            evaluator_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        TaskSubmission.seal(
            task=task,
            dod_check_run=dod,
            primary_asset_version=version,
            submission_note="Ready for human review.",
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            actor_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        self._transition(task, Task.State.SUBMITTED)
        self._transition(task, Task.State.UNDER_REVIEW)
        return task

    def _review_post(self, decision="APPROVED", command_id=None):
        self.task.refresh_from_db()
        return self.client.post(
            reverse("dashboard:review-action", args=[self.task.pk]),
            {
                "command_id": command_id or uuid.uuid4(),
                "expected_state_version": self.task.state_version,
                "decision": decision,
                "rationale": "Exact content checked by a human reviewer.",
            },
        )

    def _approve(self):
        submission = self.task.submissions.get()
        ReviewDecision.record_final(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Approved in test setup.",
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            reviewer_principal=self.reviewer,
            acting_role=self.reviewer.role,
            permission_grant=self.reviewer_review,
            recorded_by_principal=self.reviewer,
        )
        self._transition(self.task, Task.State.APPROVED, self.reviewer, self.reviewer_edit)

    def _gate_via_ui(self):
        self.client.force_login(self.publisher)
        command = uuid.uuid4()
        response = self.client.post(
            reverse("dashboard:release-gate-action", args=[self.task.pk]),
            {
                "command_id": command,
                "expected_state_version": self.task.state_version,
                "channel_account": self.channel.pk,
                "runtime_environment": self.environment.pk,
            },
        )
        return response, command, self.task.submissions.get().publications.get()

    def test_review_queue_permission_approval_and_idempotency(self):
        self.client.force_login(self.reviewer)
        queue = self.client.get(reverse("dashboard:review-queue"))
        self.assertContains(queue, self.task.title)
        command = uuid.uuid4()
        first = self._review_post(command_id=command)
        self.assertRedirects(first, reverse("dashboard:review-queue"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.APPROVED)
        replay = self.client.post(
            reverse("dashboard:review-action", args=[self.task.pk]),
            {
                "command_id": command,
                "expected_state_version": self.task.state_version - 1,
                "decision": "APPROVED",
                "rationale": "Exact content checked by a human reviewer.",
            },
        )
        self.assertRedirects(replay, reverse("dashboard:review-queue"))
        self.assertEqual(ReviewDecision.objects.filter(submission__task=self.task).count(), 1)
        self.assertEqual(self.task.state_events.filter(to_state=Task.State.APPROVED).count(), 1)

        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(reverse("dashboard:review-queue")), self.task.title)

    def test_review_requires_independent_edit_grant_and_changes_path_is_explicit(self):
        review_only = Principal.objects.create_user(
            username="review-only", password="test-only", role=Principal.Role.OPERATIONS_ADMIN
        )
        self._grant(review_only, PermissionGrant.Action.REVIEW)
        self.client.force_login(review_only)
        denied = self.client.post(
            reverse("dashboard:review-action", args=[self.task.pk]),
            {
                "command_id": uuid.uuid4(),
                "expected_state_version": self.task.state_version,
                "decision": "APPROVED",
                "rationale": "I can review but cannot project state.",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(ReviewDecision.objects.filter(submission__task=self.task).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.UNDER_REVIEW)

        self.client.force_login(self.reviewer)
        changed = self._review_post(decision="CHANGES_REQUESTED")
        self.assertRedirects(changed, reverse("dashboard:review-queue"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.HUMAN_REWORK)
        self.assertEqual(self.task.submissions.get().final_review.decision, "CHANGES_REQUESTED")

    def test_release_gate_proof_and_owner_completion_are_manual_and_idempotent(self):
        self._approve()
        response, gate_command, publication = self._gate_via_ui()
        self.assertRedirects(response, reverse("dashboard:release-detail", args=[self.task.pk]))
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)
        self.assertEqual(ReleaseGateRecord.objects.count(), 1)
        self.assertEqual(RuleEvaluationRun.objects.count(), 1)

        replay = self.client.post(
            reverse("dashboard:release-gate-action", args=[self.task.pk]),
            {
                "command_id": gate_command,
                "expected_state_version": self.task.state_version,
                "channel_account": self.channel.pk,
                "runtime_environment": self.environment.pk,
            },
        )
        self.assertRedirects(replay, reverse("dashboard:release-detail", args=[self.task.pk]))
        self.assertEqual(ReleaseGateRecord.objects.count(), 1)

        proof_command = uuid.uuid4()
        proof_payload = {
            "command_id": proof_command,
            "publication": publication.pk,
            "external_url": "https://www.quora.com/answer/manual-123",
            "external_publication_id": "manual-123",
            "proof_file": SimpleUploadedFile(
                "proof.png", b"\x89PNG\r\n\x1a\nmanual proof bytes", content_type="image/png"
            ),
        }
        proof_response = self.client.post(
            reverse("dashboard:release-proof-action", args=[self.task.pk]),
            proof_payload,
        )
        self.assertRedirects(proof_response, reverse("dashboard:release-detail", args=[self.task.pk]))
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.MANUAL_PUBLISHED_RECORDED)
        self.assertEqual(
            publication.events.filter(event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED).count(),
            1,
        )
        files_before = [item for item in Path(self.media_root).rglob("*") if item.is_file()]
        self.assertEqual(len(files_before), 1)

        replay_payload = {
            **{key: value for key, value in proof_payload.items() if key != "proof_file"},
            "proof_file": SimpleUploadedFile(
                "proof.png", b"\x89PNG\r\n\x1a\nmanual proof bytes", content_type="image/png"
            ),
        }
        proof_replay = self.client.post(
            reverse("dashboard:release-proof-action", args=[self.task.pk]),
            replay_payload,
        )
        self.assertRedirects(proof_replay, reverse("dashboard:release-detail", args=[self.task.pk]))
        self.assertEqual(
            publication.events.filter(event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED).count(),
            1,
        )
        self.assertEqual(len([item for item in Path(self.media_root).rglob("*") if item.is_file()]), 1)

        publisher_done = self.client.post(
            reverse("dashboard:release-done-action", args=[self.task.pk]),
            {"command_id": uuid.uuid4(), "expected_state_version": self.task.state_version},
        )
        self.assertEqual(publisher_done.status_code, 403)
        self.client.force_login(self.owner)
        done = self.client.post(
            reverse("dashboard:release-done-action", args=[self.task.pk]),
            {"command_id": uuid.uuid4(), "expected_state_version": self.task.state_version},
        )
        self.assertRedirects(done, reverse("dashboard:release-queue"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.DONE)

    def test_failed_proof_authorization_deletes_uploaded_file_and_writes_no_event(self):
        self._approve()
        _response, _command, publication = self._gate_via_ui()
        self.publisher_grant.grant_status = PermissionGrant.GrantStatus.REVOKED
        self.publisher_grant.save()
        before_events = PublicationEvent.objects.count()
        response = self.client.post(
            reverse("dashboard:release-proof-action", args=[self.task.pk]),
            {
                "command_id": uuid.uuid4(),
                "publication": publication.pk,
                "external_publication_id": "not-recorded",
                "proof_file": SimpleUploadedFile(
                    "failed.png", b"\x89PNG\r\n\x1a\nmust be cleaned", content_type="image/png"
                ),
            },
        )
        self.assertRedirects(response, reverse("dashboard:release-detail", args=[self.task.pk]))
        self.assertEqual(PublicationEvent.objects.count(), before_events)
        self.assertEqual([item for item in Path(self.media_root).rglob("*") if item.is_file()], [])
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)

    def test_release_queue_and_detail_hide_unauthorized_user(self):
        self._approve()
        self.client.force_login(self.publisher)
        self.assertContains(self.client.get(reverse("dashboard:release-queue")), self.task.title)
        self.assertEqual(self.client.get(reverse("dashboard:release-detail", args=[self.task.pk])).status_code, 200)
        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(reverse("dashboard:release-queue")), self.task.title)
        self.assertEqual(self.client.get(reverse("dashboard:release-detail", args=[self.task.pk])).status_code, 403)
