from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.conf import settings
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import include, path, reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAsset, ContentAssetVersion, ReviewDecision, TaskSubmission
from integrations.connectors.types import Platform
from integrations.publishing import (
    PublicationDispatchResult,
    PublicationDispatchStatus,
    PublicationMode,
    PublicationRuntime,
    PublicationRuntimeConfig,
)
from dashboard.review_views import (
    release_detail,
    release_done_action,
    release_gate_action,
    release_proof_action,
    release_queue,
    review_action,
    review_detail,
    review_history_detail,
    review_queue,
)
from dashboard.release_actions import release_rework_action, release_stop_action
from dashboard.views import home
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
    TaskStateEvent,
)


def empty_view(_request):
    return HttpResponse("ok")


dashboard_patterns = [
    path("", home, name="home"),
    path("tasks/new/", empty_view, name="task-create"),
    path("tasks/<uuid:task_id>/", empty_view, name="task-detail"),
    path("review/", review_queue, name="review-queue"),
    path("review/<uuid:task_id>/", review_detail, name="review-detail"),
    path("review/<uuid:task_id>/action/", review_action, name="review-action"),
    path(
        "review/history/<uuid:review_id>/",
        review_history_detail,
        name="review-history-detail",
    ),
    path("release/", release_queue, name="release-queue"),
    path("release/<uuid:task_id>/", release_detail, name="release-detail"),
    path("release/<uuid:task_id>/gate/", release_gate_action, name="release-gate-action"),
    path("release/<uuid:task_id>/proof/", release_proof_action, name="release-proof-action"),
    path("release/<uuid:task_id>/done/", release_done_action, name="release-done-action"),
    path("release/<uuid:task_id>/stop/", release_stop_action, name="release-stop-action"),
    path("release/<uuid:task_id>/rework/", release_rework_action, name="release-rework-action"),
]
urlpatterns = [
    path("", include((dashboard_patterns, "dashboard"), namespace="dashboard")),
    path("login/", empty_view, name="login"),
    path("logout/", empty_view, name="logout"),
]


@override_settings(ROOT_URLCONF=__name__)
class ReviewReleaseUISliceTests(TestCase):
    def setUp(self):
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
        self.owner_create = self._grant(self.owner, PermissionGrant.Action.CREATE_TASK)
        self.owner_assign = self._grant(self.owner, PermissionGrant.Action.ASSIGN_TASK)
        self.owner_cancel = self._grant(self.owner, PermissionGrant.Action.CANCEL_TASK)
        self.owner_complete = self._grant(self.owner, PermissionGrant.Action.COMPLETE_TASK)
        self.operator_edit = self._grant(self.operator, PermissionGrant.Action.EDIT)
        self.reviewer_review = self._grant(self.reviewer, PermissionGrant.Action.REVIEW)
        self.reviewer_edit = self._grant(self.reviewer, PermissionGrant.Action.EDIT)
        self.publisher_view = self._grant(self.publisher, PermissionGrant.Action.VIEW)
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
        self.asset_url = "https://docs.example.com/deliveries/ui-answer-v1"
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
        if grant is None:
            grant = {
                Task.State.ASSIGNED: self.owner_assign,
                Task.State.DONE: self.owner_complete,
            }.get(target, self.owner_edit)
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

    def _under_review_task(
        self,
        *,
        object_key=None,
        inline_content=None,
        mime_type="text/uri-list",
        metadata=None,
    ):
        object_key = object_key or self.asset_url
        if metadata is None:
            metadata = {"source": "external-url"}
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
            permission_grant=self.owner_assign,
            recorded_by_principal=self.owner,
        )
        task.refresh_from_db()
        self._transition(task, Task.State.ASSIGNED)
        self._transition(
            task,
            Task.State.IN_PROGRESS,
            principal=self.operator,
            grant=self.operator_edit,
        )
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
        if inline_content is None:
            version = ContentAssetVersion.create_next(
                content_asset=asset,
                representation_kind=ContentAssetVersion.RepresentationKind.EXTERNAL_URL,
                object_key=object_key,
                mime_type=mime_type,
                byte_size=len(object_key.encode("utf-8")),
                content_sha256=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
                metadata=metadata,
                command_id=uuid.uuid4(),
                actor_principal=self.operator,
                acting_role=self.operator.role,
                permission_grant=self.operator_edit,
                recorded_by_principal=self.operator,
            )
        else:
            version = ContentAssetVersion.create_next(
                content_asset=asset,
                representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
                inline_content=inline_content,
                mime_type="text/plain; charset=utf-8",
                metadata={"source": "generated-inline-content", "title": "Exact Quora answer"},
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

    def test_submitter_cannot_review_own_submission_even_with_both_grants(self):
        self._grant(self.operator, PermissionGrant.Action.REVIEW)
        self.client.force_login(self.operator)

        queue = self.client.get(reverse("dashboard:review-queue"))
        self.assertNotContains(queue, self.task.title)
        denied = self._review_post()

        self.assertEqual(denied.status_code, 302)
        self.assertEqual(
            denied.headers["Location"],
            reverse("dashboard:review-detail", args=[self.task.pk]),
        )
        self.assertFalse(ReviewDecision.objects.filter(submission__task=self.task).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.UNDER_REVIEW)

    def test_completed_review_history_is_read_only_and_private_to_reviewer(self):
        self.client.force_login(self.reviewer)
        response = self._review_post()
        self.assertRedirects(response, reverse("dashboard:review-queue"))
        review = ReviewDecision.objects.get(submission__task=self.task)

        queue = self.client.get(reverse("dashboard:review-queue"))
        self.assertContains(queue, "我已完成的审核")
        self.assertContains(queue, "Exact content checked by a human reviewer.")
        self.assertContains(
            queue,
            reverse("dashboard:review-history-detail", args=[review.pk]),
        )

        detail = self.client.get(
            reverse("dashboard:review-history-detail", args=[review.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "只读记录")
        self.assertContains(detail, review.submission.primary_asset_version.content_sha256)
        self.assertContains(detail, self.asset_url)
        self.assertNotContains(detail, 'class="task-action-form"')

        self.client.force_login(self.outsider)
        hidden = self.client.get(
            reverse("dashboard:review-history-detail", args=[review.pk])
        )
        self.assertEqual(hidden.status_code, 404)

    def test_review_page_opens_exact_external_url_without_a_download_route(self):
        self.client.force_login(self.reviewer)
        detail = self.client.get(reverse("dashboard:review-detail", args=[self.task.pk]))
        self.assertContains(detail, f'href="{self.asset_url}"')
        self.assertContains(detail, "打开这份交付内容")
        self.assertNotContains(detail, "/files/assets/")

    def test_review_page_displays_exact_inline_content(self):
        inline_task = self._under_review_task(
            inline_content="Exact hook\n\nExact reviewed body\n\nExact CTA",
        )
        self.client.force_login(self.reviewer)

        detail = self.client.get(reverse("dashboard:review-detail", args=[inline_task.pk]))

        self.assertContains(detail, "Exact reviewed body")
        self.assertContains(detail, "复制完整内容")
        self.assertContains(detail, "精确送审内容")
        self.assertNotContains(detail, "V1 已停止支持该旧文件交付")

    def test_review_page_does_not_turn_a_legacy_object_key_into_a_link(self):
        legacy_object_key = "https://legacy.example/answer.txt"
        legacy_task = self._under_review_task(
            object_key=legacy_object_key,
            mime_type="text/plain",
            metadata={"original_filename": "answer.txt"},
        )
        self.client.force_login(self.reviewer)

        detail = self.client.get(
            reverse("dashboard:review-detail", args=[legacy_task.pk])
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "V1 已停止支持该旧文件交付")
        self.assertNotContains(detail, f'href="{legacy_object_key}"')

    def test_historical_reviewer_requires_current_grant_to_open_external_url(self):
        self.client.force_login(self.reviewer)
        self._review_post()
        review = ReviewDecision.objects.get(submission__task=self.task)
        history_url = reverse("dashboard:review-history-detail", args=[review.pk])
        allowed = self.client.get(history_url)
        self.assertContains(allowed, f'href="{self.asset_url}"')
        self.reviewer_review.grant_status = PermissionGrant.GrantStatus.REVOKED
        self.reviewer_review.revoked_at = timezone.now()
        self.reviewer_review.revoked_by_principal = self.owner
        self.reviewer_review.revocation_reason = "Historical access removal test."
        self.reviewer_review.save(
            update_fields=[
                "grant_status",
                "revoked_at",
                "revoked_by_principal",
                "revocation_reason",
                "updated_at",
            ]
        )

        response = self.client.get(history_url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'href="{self.asset_url}"')
        self.assertNotContains(response, self.asset_url)
        self.assertContains(response, "不能再打开交付链接")

    def test_release_page_opens_exact_external_url(self):
        self._approve()
        self._gate_via_ui()
        response = self.client.get(reverse("dashboard:release-detail", args=[self.task.pk]))
        self.assertContains(response, f'href="{self.asset_url}"')
        self.assertContains(response, "不能继续发布：这次审核通过的只有一个外部链接")
        self.assertContains(response, "查看这次提交的外部链接")
        self.assertContains(response, "请先补齐完整正文")
        self.assertNotContains(response, 'name="external_url"')
        self.assertNotContains(response, 'name="external_publication_id"')
        self.assertNotContains(response, "/files/assets/")

    def test_authorized_owner_can_return_link_only_submission_for_full_content(self):
        self._approve()
        self.client.force_login(self.owner)

        response = self.client.get(reverse("dashboard:release-detail", args=[self.task.pk]))

        self.assertContains(response, "退回制作完整正文")
        self.assertContains(
            response,
            reverse("dashboard:release-rework-action", args=[self.task.pk]),
        )
        self.assertContains(response, 'name="reason"')
        self.assertContains(response, 'name="confirmed"')

    def test_owner_returns_approved_link_then_seals_inline_v2_without_reusing_old_gate(self):
        self._approve()
        _gate_response, _gate_command, publication = self._gate_via_ui()
        old_submission = self.task.submissions.get()
        old_review = old_submission.final_review
        old_gate = publication.current_gate
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("dashboard:release-rework-action", args=[self.task.pk]),
            {
                "command_id": uuid.uuid4(),
                "expected_state_version": self.task.state_version,
                "reason": "The approved link must be replaced with complete inline copy.",
                "confirmed": "on",
            },
        )

        self.assertRedirects(
            response,
            reverse("dashboard:task-detail", args=[self.task.pk]),
            fetch_redirect_response=False,
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.HUMAN_REWORK)
        self.assertTrue(
            self.task.state_events.filter(
                event_type=TaskStateEvent.EventType.APPROVED_REWORK_REQUESTED,
                submission=old_submission,
                permission_grant=self.owner_edit,
            ).exists()
        )
        self.assertIn("TASK_NOT_APPROVED", old_gate.current_blockers())
        self.assertTrue(TaskSubmission.objects.filter(pk=old_submission.pk).exists())
        self.assertTrue(ReviewDecision.objects.filter(pk=old_review.pk).exists())
        self.assertTrue(ReleaseGateRecord.objects.filter(pk=old_gate.pk).exists())

        self._transition(
            self.task,
            Task.State.IN_PROGRESS,
            principal=self.operator,
            grant=self.operator_edit,
        )
        inline_v2 = ContentAssetVersion.create_next(
            content_asset=old_submission.primary_asset_version.content_asset,
            representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
            inline_content="Complete replacement copy with a hook, body and call to action.",
            mime_type="text/plain; charset=utf-8",
            metadata={"source": "approved-rework-test"},
            command_id=uuid.uuid4(),
            actor_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        dod_v2 = TaskCheckRun.record_completed(
            task=self.task,
            check_kind=TaskCheckRun.Kind.DOD,
            results=[
                {
                    "criterion_key": "complete",
                    "result": "PASS",
                    "evidence": {"version": str(inline_v2.pk)},
                }
            ],
            command_id=uuid.uuid4(),
            evaluator_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        new_submission = TaskSubmission.seal(
            task=self.task,
            dod_check_run=dod_v2,
            primary_asset_version=inline_v2,
            submission_note="Complete inline replacement.",
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            actor_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
            supersedes_submission=old_submission,
            triggering_review=old_review,
        )
        self.assertEqual(new_submission.submission_number, 2)
        self.assertEqual(new_submission.supersedes_submission_id, old_submission.pk)
        self.assertEqual(inline_v2.version_number, 2)
        self.assertEqual(
            new_submission.primary_asset_version.representation_kind,
            ContentAssetVersion.RepresentationKind.INLINE_TEXT,
        )

    def test_owner_stops_approved_publication_and_old_gate_becomes_unusable(self):
        self.task = self._under_review_task(
            inline_content="Approved hook\n\nApproved body\n\nApproved CTA",
        )
        self._approve()
        _gate_response, _gate_command, publication = self._gate_via_ui()
        old_gate = publication.current_gate
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("dashboard:release-stop-action", args=[self.task.pk]),
            {
                "command_id": uuid.uuid4(),
                "expected_state_version": self.task.state_version,
                "reason": "The publication is no longer needed.",
                "confirmed": "on",
            },
        )

        self.assertRedirects(response, reverse("dashboard:release-queue"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.CANCELLED)
        stop_event = self.task.state_events.get(to_state=Task.State.CANCELLED)
        self.assertEqual(stop_event.permission_grant_id, self.owner_cancel.pk)
        self.assertEqual(stop_event.reason, "The publication is no longer needed.")
        self.assertIn("TASK_NOT_APPROVED", old_gate.current_blockers())
        self.assertTrue(ReleaseGateRecord.objects.filter(pk=old_gate.pk).exists())

    def test_compiled_daily_external_url_gate_cannot_be_used_by_direct_proof_post(self):
        self._approve()
        _gate_response, _gate_command, publication = self._gate_via_ui()
        before_events = PublicationEvent.objects.count()
        self.client.force_login(self.publisher)

        with patch(
            "intelligence.models.TaskCompilationContext.objects.filter"
        ) as compiled_filter:
            compiled_filter.return_value.exists.return_value = True
            response = self.client.post(
                reverse("dashboard:release-proof-action", args=[self.task.pk]),
                {
                    "command_id": uuid.uuid4(),
                    "publication": publication.pk,
                    "mode": "MANUAL",
                    "confirmed": "on",
                    "external_publication_id": "must-not-be-recorded",
                },
            )

        self.assertRedirects(
            response,
            reverse("dashboard:release-detail", args=[self.task.pk]),
        )
        self.assertEqual(PublicationEvent.objects.count(), before_events)
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)

    def test_release_page_displays_copyable_exact_inline_content(self):
        inline_task = self._under_review_task(
            inline_content="Approved hook\n\nApproved body\n\nApproved CTA",
        )
        self.task = inline_task
        self._approve()
        self._gate_via_ui()

        response = self.client.get(reverse("dashboard:release-detail", args=[inline_task.pk]))

        self.assertContains(response, "Approved body")
        self.assertContains(response, "复制完整正文")
        self.assertContains(response, "已经审核通过、准备发布的完整正文")
        self.assertContains(response, "发布检查已通过")
        self.assertNotContains(response, "V1 已停止支持该旧文件交付")

    def test_publish_grant_without_independent_view_cannot_reveal_inline_content(self):
        self.task = self._under_review_task(
            inline_content="Sensitive approved copy that requires VIEW",
        )
        self._approve()
        self.publisher_view.grant_status = PermissionGrant.GrantStatus.REVOKED
        self.publisher_view.revoked_at = timezone.now()
        self.publisher_view.revoked_by_principal = self.owner
        self.publisher_view.revocation_reason = "Verify PUBLISH does not imply VIEW."
        self.publisher_view.save(
            update_fields=[
                "grant_status",
                "revoked_at",
                "revoked_by_principal",
                "revocation_reason",
                "updated_at",
            ]
        )
        self.client.force_login(self.publisher)

        response = self.client.get(reverse("dashboard:release-detail", args=[self.task.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "你当前没有权限查看这份正文")
        self.assertNotContains(response, "Sensitive approved copy that requires VIEW")
        self.assertNotContains(response, ">运行发布检查</button>")

    def test_release_page_exposes_three_controlled_modes_and_explicit_confirmation(self):
        self.task = self._under_review_task(
            inline_content="Approved hook\n\nApproved body\n\nApproved CTA",
        )
        self._approve()
        self._gate_via_ui()

        response = self.client.get(reverse("dashboard:release-detail", args=[self.task.pk]))

        self.assertContains(response, 'value="MANUAL"')
        self.assertContains(response, 'value="API"')
        self.assertContains(response, 'value="BROWSER"')
        self.assertContains(response, 'name="confirmed"')
        self.assertContains(response, "我已经在平台发布，继续登记结果")
        self.assertContains(response, "保存发布结果")
        self.assertContains(response, "去平台发布")

    def test_valid_release_gate_is_a_persistent_status_before_proof(self):
        self.task = self._under_review_task(
            inline_content="Approved hook\n\nApproved body\n\nApproved CTA",
        )
        self._approve()
        self._gate_via_ui()

        response = self.client.get(reverse("dashboard:release-detail", args=[self.task.pk]))

        self.assertContains(response, "发布检查已通过")
        self.assertContains(response, self.channel.display_name)
        self.assertContains(response, self.environment.environment_code)
        self.assertContains(response, "检查时间")
        self.assertContains(response, "需要时重新运行发布检查")
        self.assertContains(response, "复制上面的正文，前往对应平台人工发布")
        self.assertContains(response, 'name="external_url"')
        self.assertContains(response, 'name="external_publication_id"')

    def test_stale_release_gate_hides_publishing_proof_until_rechecked(self):
        self.task = self._under_review_task(
            inline_content="Approved hook\n\nApproved body\n\nApproved CTA",
        )
        self._approve()
        self._gate_via_ui()
        CapabilityState.objects.create(
            account_environment_binding=self.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=2,
            state=CapabilityState.State.CLOSED,
            supersedes=self.capability,
            reason="UX test safety stop",
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )

        response = self.client.get(reverse("dashboard:release-detail", args=[self.task.pk]))

        self.assertContains(response, "之前的发布检查已经失效")
        self.assertContains(response, "MANUAL_PUBLISH_CAPABILITY_NOT_OPEN")
        self.assertNotContains(response, 'name="external_url"')
        self.assertNotContains(response, 'name="external_publication_id"')
        self.assertNotContains(response, "我已经在平台发布，继续登记结果")

    def test_release_requires_confirmation_and_rejects_client_supplied_api_proof(self):
        self._approve()
        _response, _command, publication = self._gate_via_ui()
        before_events = PublicationEvent.objects.count()

        missing_confirmation = self.client.post(
            reverse("dashboard:release-proof-action", args=[self.task.pk]),
            {
                "command_id": uuid.uuid4(),
                "publication": publication.pk,
                "mode": "MANUAL",
                "external_publication_id": "must-not-be-recorded",
            },
        )
        self.assertEqual(missing_confirmation.status_code, 400)
        self.assertContains(
            missing_confirmation,
            "不能继续发布：这次审核通过的只有一个外部链接",
            status_code=400,
        )
        self.assertEqual(PublicationEvent.objects.count(), before_events)

        forged_api_result = self.client.post(
            reverse("dashboard:release-proof-action", args=[self.task.pk]),
            {
                "command_id": uuid.uuid4(),
                "publication": publication.pk,
                "mode": "API",
                "confirmed": "on",
                "external_url": "https://attacker.example/fake-result",
            },
        )
        self.assertEqual(forged_api_result.status_code, 400)
        self.assertContains(
            forged_api_result,
            "不能继续发布：这次审核通过的只有一个外部链接",
            status_code=400,
        )
        self.assertEqual(PublicationEvent.objects.count(), before_events)

    def test_api_and_browser_paths_are_default_disabled_and_fail_closed(self):
        self._approve()
        _response, _command, publication = self._gate_via_ui()
        before_events = PublicationEvent.objects.count()

        for mode in ("API", "BROWSER"):
            with self.subTest(mode=mode):
                response = self.client.post(
                    reverse("dashboard:release-proof-action", args=[self.task.pk]),
                    {
                        "command_id": uuid.uuid4(),
                        "publication": publication.pk,
                        "mode": mode,
                        "confirmed": "on",
                    },
                )
                self.assertRedirects(
                    response,
                    reverse("dashboard:release-detail", args=[self.task.pk]),
                )
                publication.refresh_from_db()
                self.assertEqual(publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)
                self.assertEqual(PublicationEvent.objects.count(), before_events)

    def test_browser_path_uses_explicitly_injected_runtime(self):
        self._approve()
        _response, _command, publication = self._gate_via_ui()

        class SuccessfulBrowserTransport:
            def __init__(self):
                self.calls = []

            def dispatch(self, request):
                self.calls.append(request)
                return PublicationDispatchResult(
                    platform=request.platform,
                    mode=request.mode,
                    provider="fake-quora-browser-worker",
                    status=PublicationDispatchStatus.SUCCEEDED,
                    operation_key=request.operation_key,
                    external_url="https://www.quora.com/answer/fake-browser-proof",
                    external_publication_id="fake-browser-proof",
                )

        transport = SuccessfulBrowserTransport()
        runtime = PublicationRuntime(
            PublicationRuntimeConfig(
                {(Platform.QUORA, PublicationMode.BROWSER): transport}
            )
        )
        command_id = uuid.uuid4()
        with patch(
            "dashboard.review_views.get_publication_runtime",
            return_value=runtime,
        ) as runtime_factory:
            response = self.client.post(
                reverse("dashboard:release-proof-action", args=[self.task.pk]),
                {
                    "command_id": command_id,
                    "publication": publication.pk,
                    "mode": "BROWSER",
                    "confirmed": "on",
                },
            )

        self.assertRedirects(
            response, reverse("dashboard:release-detail", args=[self.task.pk])
        )
        runtime_factory.assert_called_once_with()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0].operation_key, str(command_id))
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.MANUAL_PUBLISHED_RECORDED)
        self.assertTrue(
            publication.events.filter(
                command_id=command_id,
                event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED,
            ).exists()
        )

    def test_manual_path_never_loads_the_network_runtime_factory(self):
        self._approve()
        _response, _command, publication = self._gate_via_ui()

        with patch(
            "dashboard.review_views.get_publication_runtime",
            side_effect=AssertionError("manual mode must not build a network runtime"),
        ) as runtime_factory:
            response = self.client.post(
                reverse("dashboard:release-proof-action", args=[self.task.pk]),
                {
                    "command_id": uuid.uuid4(),
                    "publication": publication.pk,
                    "mode": "MANUAL",
                    "confirmed": "on",
                    "external_publication_id": "manual-without-runtime",
                },
            )

        self.assertRedirects(
            response, reverse("dashboard:release-detail", args=[self.task.pk])
        )
        runtime_factory.assert_not_called()
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.MANUAL_PUBLISHED_RECORDED)

    @override_settings(
        PUBLICATION_NETWORK_ENABLED=True,
        PUBLICATION_RUNTIME_FACTORY="does.not.exist",
    )
    def test_invalid_runtime_factory_rejects_browser_without_an_event(self):
        self._approve()
        _response, _command, publication = self._gate_via_ui()
        before_events = PublicationEvent.objects.count()

        response = self.client.post(
            reverse("dashboard:release-proof-action", args=[self.task.pk]),
            {
                "command_id": uuid.uuid4(),
                "publication": publication.pk,
                "mode": "BROWSER",
                "confirmed": "on",
            },
        )

        self.assertRedirects(
            response, reverse("dashboard:release-detail", args=[self.task.pk])
        )
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)
        self.assertEqual(PublicationEvent.objects.count(), before_events)

    def test_today_action_counts_are_permission_filtered_at_every_stage(self):
        self.client.force_login(self.outsider)
        outsider_before = self.client.get(reverse("dashboard:home"))
        self.assertEqual(outsider_before.context["pending_review_count"], 0)
        self.assertEqual(outsider_before.context["pending_publish_count"], 0)
        self.assertEqual(outsider_before.context["pending_complete_count"], 0)

        self.client.force_login(self.reviewer)
        reviewer_today = self.client.get(reverse("dashboard:home"))
        self.assertEqual(reviewer_today.context["pending_review_count"], 1)
        self.assertEqual(reviewer_today.context["pending_publish_count"], 0)
        self.assertEqual(reviewer_today.context["pending_complete_count"], 0)
        self.assertContains(reviewer_today, "等我审核")
        self.assertContains(reviewer_today, 'class="notification-badge">1</span>')

        self._approve()
        self.client.force_login(self.publisher)
        publisher_today = self.client.get(reverse("dashboard:home"))
        self.assertEqual(publisher_today.context["pending_review_count"], 0)
        self.assertEqual(publisher_today.context["pending_publish_count"], 1)
        self.assertEqual(publisher_today.context["pending_complete_count"], 0)
        self.assertNotContains(publisher_today, "等我审核")
        self.assertContains(publisher_today, "等我发布")

        _gate_response, _gate_command, publication = self._gate_via_ui()
        proof = self.client.post(
            reverse("dashboard:release-proof-action", args=[self.task.pk]),
            {
                "command_id": uuid.uuid4(),
                "publication": publication.pk,
                "mode": "MANUAL",
                "confirmed": "on",
                "external_publication_id": "today-count-proof",
            },
        )
        self.assertRedirects(
            proof,
            reverse("dashboard:release-detail", args=[self.task.pk]),
        )

        self.client.force_login(self.owner)
        owner_today = self.client.get(reverse("dashboard:home"))
        self.assertEqual(owner_today.context["pending_review_count"], 0)
        self.assertEqual(owner_today.context["pending_publish_count"], 0)
        self.assertEqual(owner_today.context["pending_complete_count"], 1)

        self.client.force_login(self.outsider)
        outsider_after = self.client.get(reverse("dashboard:home"))
        self.assertEqual(outsider_after.context["pending_review_count"], 0)
        self.assertEqual(outsider_after.context["pending_publish_count"], 0)
        self.assertEqual(outsider_after.context["pending_complete_count"], 0)
        self.assertNotContains(outsider_after, "等我审核")
        self.assertNotContains(outsider_after, "等我发布")

    def test_owner_admin_and_operator_each_see_publish_work_with_their_own_exact_grant(self):
        now = timezone.now()
        for principal in (self.owner, self.reviewer):
            PermissionGrant.objects.create(
                principal=principal,
                scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                account_ref=self.channel.account_code,
                action=PermissionGrant.Action.PUBLISH,
                effect=PermissionGrant.Effect.ALLOW,
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(hours=1),
                granted_by_principal=self.owner,
            )
        self._approve()

        for principal in (self.owner, self.reviewer, self.publisher):
            with self.subTest(role=principal.role):
                self.client.force_login(principal)
                today = self.client.get(reverse("dashboard:home"))
                self.assertEqual(today.context["pending_publish_count"], 1)
                self.assertContains(today, "等我发布")
                self.assertContains(today, 'class="notification-badge">1</span>')
                queue = self.client.get(reverse("dashboard:release-queue"))
                self.assertContains(queue, self.task.title)

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
            "mode": "MANUAL",
            "confirmed": "on",
            "external_url": "https://www.quora.com/answer/manual-123",
            "external_publication_id": "manual-123",
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
        proof_event = publication.events.get(
            event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED
        )
        self.assertEqual(proof_event.external_url, proof_payload["external_url"])
        self.assertEqual(proof_event.external_publication_id, "manual-123")
        self.assertEqual(proof_event.proof_reference, "")
        self.assertEqual(proof_event.proof_sha256, "")

        proof_replay = self.client.post(
            reverse("dashboard:release-proof-action", args=[self.task.pk]),
            proof_payload,
        )
        self.assertRedirects(proof_replay, reverse("dashboard:release-detail", args=[self.task.pk]))
        self.assertEqual(
            publication.events.filter(event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED).count(),
            1,
        )

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

    def test_publication_credential_is_visible_only_to_requester_and_completion_authority(self):
        self._approve()
        _response, _command, publication = self._gate_via_ui()
        published_url = "https://www.quora.com/answer/private-proof"
        proof_response = self.client.post(
            reverse("dashboard:release-proof-action", args=[self.task.pk]),
            {
                "command_id": uuid.uuid4(),
                "publication": publication.pk,
                "mode": "MANUAL",
                "confirmed": "on",
                "external_publication_id": "private-proof",
                "external_url": published_url,
            },
        )
        self.assertRedirects(
            proof_response,
            reverse("dashboard:release-detail", args=[self.task.pk]),
        )
        publication.events.get(
            event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED
        )
        detail_url = reverse("dashboard:release-detail", args=[self.task.pk])
        requester_response = self.client.get(detail_url)
        self.assertEqual(requester_response.status_code, 200)
        self.assertContains(requester_response, published_url)
        self.assertContains(requester_response, "private-proof")
        self.assertNotContains(requester_response, "/files/publication-events/")

        self.client.force_login(self.owner)
        owner_response = self.client.get(detail_url)
        self.assertEqual(owner_response.status_code, 200)
        self.assertContains(owner_response, published_url)
        self.assertContains(owner_response, "private-proof")

        expired_at = self.publisher_grant.valid_until + timedelta(seconds=1)
        self.client.force_login(self.publisher)
        with patch("accounts.authorization.timezone.now", return_value=expired_at):
            stale_publisher_response = self.client.get(detail_url)
        self.assertEqual(stale_publisher_response.status_code, 200)
        self.assertNotContains(stale_publisher_response, f'href="{published_url}"')
        self.assertNotContains(stale_publisher_response, "private-proof")

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(detail_url).status_code, 403)

    def test_expired_publish_grant_after_ready_writes_no_publication_event(self):
        self._approve()
        _response, _command, publication = self._gate_via_ui()
        expired_at = self.publisher_grant.valid_until + timedelta(seconds=1)
        before_events = PublicationEvent.objects.count()
        with patch("accounts.authorization.timezone.now", return_value=expired_at), patch(
            "releasegate.services.timezone.now", return_value=expired_at
        ):
            response = self.client.post(
                reverse("dashboard:release-proof-action", args=[self.task.pk]),
                {
                    "command_id": uuid.uuid4(),
                    "publication": publication.pk,
                    "mode": "MANUAL",
                    "confirmed": "on",
                    "external_publication_id": "not-recorded",
                },
            )
        self.assertRedirects(response, reverse("dashboard:release-detail", args=[self.task.pk]))
        self.assertEqual(PublicationEvent.objects.count(), before_events)
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.READY_FOR_MANUAL_PUBLISH)

    def test_publication_service_failure_writes_no_event(self):
        self._approve()
        _response, _command, publication = self._gate_via_ui()
        root_command = uuid.uuid4()

        with patch(
            "dashboard.review_views.dispatch_confirmed_publication",
            side_effect=ValidationError("Simulated publication credential failure."),
        ):
            response = self.client.post(
                reverse("dashboard:release-proof-action", args=[self.task.pk]),
                {
                    "command_id": root_command,
                    "publication": publication.pk,
                    "mode": "MANUAL",
                    "confirmed": "on",
                    "external_publication_id": "must-not-be-recorded",
                },
            )

        self.assertRedirects(
            response,
            reverse("dashboard:release-detail", args=[self.task.pk]),
            fetch_redirect_response=False,
        )
        self.assertFalse(PublicationEvent.objects.filter(command_id=root_command).exists())

    def test_release_queue_and_detail_hide_unauthorized_user(self):
        self._approve()
        self.client.force_login(self.publisher)
        self.assertContains(self.client.get(reverse("dashboard:release-queue")), self.task.title)
        self.assertEqual(self.client.get(reverse("dashboard:release-detail", args=[self.task.pk])).status_code, 200)
        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(reverse("dashboard:release-queue")), self.task.title)
        self.assertEqual(self.client.get(reverse("dashboard:release-detail", args=[self.task.pk])).status_code, 403)

    def test_review_and_release_core_controls_switch_fully_to_english(self):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        self.client.force_login(self.reviewer)

        review_queue_response = self.client.get(reverse("dashboard:review-queue"))
        self.assertContains(review_queue_response, "Review queue")
        self.assertContains(review_queue_response, "Open review")
        self.assertContains(review_queue_response, "My completed reviews")
        self.assertNotContains(review_queue_response, "打开审核")
        self.assertNotContains(review_queue_response, "我已完成的审核")

        review_detail_response = self.client.get(
            reverse("dashboard:review-detail", args=[self.task.pk])
        )
        self.assertContains(review_detail_response, "Human review")
        self.assertContains(review_detail_response, "Review decision")
        self.assertContains(review_detail_response, "Save review decision")
        self.assertContains(review_detail_response, "Approve and continue to release checks")
        self.assertNotContains(review_detail_response, "保存审核结论")
        self.assertNotContains(review_detail_response, "审核说明")

        self.task = self._under_review_task(
            inline_content="Approved hook\n\nApproved body\n\nApproved CTA",
        )
        self._approve()
        self.client.force_login(self.publisher)
        release_queue_response = self.client.get(reverse("dashboard:release-queue"))
        self.assertContains(release_queue_response, "Publishing queue")
        self.assertContains(release_queue_response, "Open publishing flow")
        self.assertNotContains(release_queue_response, "打开发布流程")

        release_detail_response = self.client.get(
            reverse("dashboard:release-detail", args=[self.task.pk])
        )
        self.assertContains(release_detail_response, "Release checks and manual publishing")
        self.assertContains(release_detail_response, "Step 1 · Review the content")
        self.assertContains(release_detail_response, "Publishing account")
        self.assertContains(release_detail_response, "Run release checks")
        self.assertNotContains(release_detail_response, "运行发布检查")
        self.assertNotContains(release_detail_response, "发布账号")
