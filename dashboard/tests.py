import hashlib
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAsset, ContentAssetVersion, ReviewDecision, TaskSubmission
from products.models import Product, ProductProfileVersion
from workflow.models import (
    ActingRole,
    Task,
    TaskAssignment,
    TaskCheckRun,
    TaskContractVersion,
    TaskStateEvent,
)


class DashboardTests(TestCase):
    def setUp(self):
        self.user = Principal.objects.create_user(username="operator", password="local-test-only")
        self.other_user = Principal.objects.create_user(username="another", password="local-test-only")

    def make_task(self, *, owner, title="Write a clear Quora answer", assignee=None):
        assignee = assignee or owner
        product = Product.objects.create(
            product_code=f"P-{Product.objects.count() + 1}",
            name="PUKO Magnesium",
            market_code="US",
            language_code="en",
            created_by_principal=owner,
            updated_by_principal=owner,
        )
        profile = ProductProfileVersion.objects.create(
            product=product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"need": "plain-language wellness information"},
            core_value_proposition="Evidence-informed daily wellness.",
            brand_voice={"tone": "clear"},
            product_facts={},
            prohibited_expressions=[],
            created_by_principal=owner,
        )
        profile.seal(owner)
        contract = TaskContractVersion.objects.create(
            product_profile_version=profile,
            version_number=1,
            title="Quora answer task",
            dor_criteria=[{"key": "source_ready", "label": "Reliable sources are ready"}],
            dod_criteria=[{"key": "plain_language", "label": "Use plain, natural language"}],
            release_gate_criteria=[{"key": "human_review", "label": "Human review must pass"}],
            success_criteria=[{"key": "published_result", "required": False}],
            sealed_at=timezone.now(),
            created_by_principal=owner,
        )
        task = Task.objects.create(
            product=product,
            product_profile_version=profile,
            contract_version=contract,
            title=title,
            description="Answer a real user question in language that is easy to understand.",
            created_by_principal=owner,
            updated_by_principal=owner,
        )
        def grant(principal, action):
            return PermissionGrant.objects.create(
                principal=principal,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                product=product,
                action=action,
                effect=PermissionGrant.Effect.ALLOW,
                valid_from=timezone.now() - timedelta(minutes=1),
                valid_until=timezone.now() + timedelta(hours=1),
                granted_by_principal=owner,
            )

        owner_edit = grant(owner, PermissionGrant.Action.EDIT)
        owner_assign = grant(owner, PermissionGrant.Action.ASSIGN_TASK)
        assignee_edit = owner_edit if assignee.pk == owner.pk else grant(
            assignee, PermissionGrant.Action.EDIT
        )
        TaskCheckRun.record_completed(
            task=task,
            check_kind=TaskCheckRun.Kind.DOR,
            results=[{
                "criterion_key": "source_ready",
                "result": TaskCheckRun.Result.PASS,
                "evidence": {"source_ready": True},
            }],
            command_id=uuid.uuid4(),
            evaluator_principal=owner,
            acting_role=ActingRole.OPERATOR,
            permission_grant=owner_edit,
            recorded_by_principal=owner,
        )
        Task.transition(
            task_id=task.pk,
            to_state=Task.State.READY,
            command_id=uuid.uuid4(),
            expected_state_version=0,
            actor_principal=owner,
            acting_role=ActingRole.OPERATOR,
            permission_grant=owner_edit,
            recorded_by_principal=owner,
        )
        task.refresh_from_db()
        TaskAssignment.record(
            task=task,
            assignee_principal=assignee,
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            assigned_by_principal=owner,
            acting_role=ActingRole.OPERATOR,
            permission_grant=owner_assign,
            recorded_by_principal=owner,
        )
        Task.transition(
            task_id=task.pk,
            to_state=Task.State.ASSIGNED,
            command_id=uuid.uuid4(),
            expected_state_version=task.state_version,
            actor_principal=owner,
            acting_role=ActingRole.OPERATOR,
            permission_grant=owner_assign,
            recorded_by_principal=owner,
        )
        task.refresh_from_db()
        Task.transition(
            task_id=task.pk,
            to_state=Task.State.IN_PROGRESS,
            command_id=uuid.uuid4(),
            expected_state_version=task.state_version,
            actor_principal=assignee,
            acting_role=assignee.role,
            permission_grant=assignee_edit,
            recorded_by_principal=assignee,
        )
        task.refresh_from_db()
        return task

    def test_anonymous_user_is_sent_to_login(self):
        response = self.client.get(reverse("dashboard:home"))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_authenticated_user_sees_empty_state(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:home"))
        self.assertContains(response, "PUKO Growth OS")
        self.assertContains(response, "现在没有执行任务")
        self.assertContains(response, "今天的待办已经清空")
        self.assertEqual(response.context["task_count"], 0)

    def test_user_only_sees_tasks_assigned_to_or_created_by_them(self):
        assigned = self.make_task(owner=self.other_user, title="Assigned to me", assignee=self.user)
        created = self.make_task(owner=self.user, title="Created by me")
        hidden = self.make_task(owner=self.other_user, title="Another user's task", assignee=self.other_user)

        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:home"))

        visible_ids = {task.id for task in response.context["tasks"]}
        self.assertEqual(visible_ids, {assigned.id, created.id})
        self.assertNotIn(hidden.id, visible_ids)
        self.assertNotContains(response, "Another user's task")

    def test_task_row_leads_with_plain_next_action_and_hides_technical_detail(self):
        self.make_task(owner=self.user, assignee=self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard:home"))

        self.assertContains(response, "Write a clear Quora answer")
        self.assertContains(response, "进行中")
        self.assertContains(response, "继续并提交")
        self.assertNotContains(response, "开工检查（DoR）")
        self.assertNotContains(response, "交付检查（DoD）")
        self.assertNotContains(response, "合同版本")

    def test_creator_does_not_see_work_that_is_currently_assigned_to_someone_else(self):
        delegated = self.make_task(
            owner=self.user,
            title="Delegated work",
            assignee=self.other_user,
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:home"))

        self.assertNotIn(delegated.pk, {task.pk for task in response.context["tasks"]})
        self.assertNotContains(response, "Delegated work")

    def test_assigned_task_disappears_immediately_after_edit_grant_is_revoked(self):
        assigned = self.make_task(
            owner=self.other_user,
            title="Permission filtered work",
            assignee=self.user,
        )
        PermissionGrant.objects.filter(
            principal=self.user,
            product=assigned.product,
            action=PermissionGrant.Action.EDIT,
        ).update(
            grant_status=PermissionGrant.GrantStatus.REVOKED,
            revoked_at=timezone.now(),
            revoked_by_principal=self.other_user,
            revocation_reason="Access removed before inbox refresh.",
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:home"))

        self.assertEqual(response.context["task_count"], 0)
        self.assertNotContains(response, "Permission filtered work")
        self.assertEqual(response.context["action_center"].total_count, 0)


class ControlledTaskUiTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="ops-admin",
            password="local-test-only",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.operator = Principal.objects.create_user(
            username="delivery-operator",
            password="local-test-only",
            role=Principal.Role.OPERATOR,
        )
        self.outsider = Principal.objects.create_user(
            username="no-task-access",
            password="local-test-only",
            role=Principal.Role.OPERATOR,
        )
        self.product = Product.objects.create(
            product_code="PUKO-CONTROLLED-UI",
            name="PUKO Magnesium",
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
            audience={"need": "plain-language wellness information"},
            core_value_proposition="Evidence-informed daily wellness.",
            brand_voice={"tone": "clear"},
            product_facts={},
            prohibited_expressions=[],
            created_by_principal=self.owner,
        )
        self.profile.seal(self.owner)
        self.product.current_profile_version = self.profile
        self.product.save(update_fields=["current_profile_version", "updated_at"])
        self.contract = TaskContractVersion.objects.create(
            product_profile_version=self.profile,
            version_number=1,
            title="Controlled content task",
            dor_criteria=[{"key": "source_ready", "label": "Reliable sources are ready"}],
            dod_criteria=[{"key": "plain_language", "label": "Use plain, natural language"}],
            release_gate_criteria=[{"key": "human_review", "label": "Human review must pass"}],
            success_criteria=[{"key": "published_result", "required": False}],
            sealed_at=timezone.now(),
            created_by_principal=self.owner,
        )
        self.task = self._new_task(self.owner)
        self.owner_grant = self._grant_edit(self.owner)
        self.owner_create_grant = self._grant(self.owner, PermissionGrant.Action.CREATE_TASK)
        self.owner_assign_grant = self._grant(self.owner, PermissionGrant.Action.ASSIGN_TASK)
        self.owner_cancel_grant = self._grant(self.owner, PermissionGrant.Action.CANCEL_TASK)
        self.owner_complete_grant = self._grant(self.owner, PermissionGrant.Action.COMPLETE_TASK)
        self.operator_grant = self._grant_edit(self.operator)

    def _new_task(self, creator):
        return Task.objects.create(
            product=self.product,
            product_profile_version=self.profile,
            contract_version=self.contract,
            title="Prepare one controlled answer",
            description="Turn verified evidence into a useful answer.",
            created_by_principal=creator,
            updated_by_principal=creator,
        )

    def _grant_edit(self, principal):
        return self._grant(principal, PermissionGrant.Action.EDIT)

    def _grant(self, principal, action):
        return PermissionGrant.objects.create(
            principal=principal,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=action,
            effect=PermissionGrant.Effect.ALLOW,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=1),
            granted_by_principal=self.owner,
        )

    @staticmethod
    def _command_data(task, **extra):
        task.refresh_from_db()
        return {
            "command_id": str(uuid.uuid4()),
            "expected_state_version": task.state_version,
            **extra,
        }

    def _pass_dor(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "dor"]),
            self._command_data(self.task, criterion__source_ready=TaskCheckRun.Result.PASS),
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.READY)

    def _assign_and_start(self):
        self._pass_dor()
        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "assign"]),
            self._command_data(self.task, assignee=str(self.operator.pk)),
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.ASSIGNED)
        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "start"]),
            self._command_data(self.task),
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.IN_PROGRESS)

    def test_task_detail_is_private_and_shows_only_the_current_controlled_action(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("dashboard:task-detail", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "逐项确认：现在是否具备开工条件？")
        self.assertContains(response, "Reliable sources are ready")
        self.assertContains(response, "本页不会审核、通过门禁或发布内容")
        self.assertNotContains(response, "Daily Operations 编译上下文（只读）")
        self.assertNotContains(response, "/actions/review/")
        self.assertNotContains(response, "/actions/gate/")
        self.assertNotContains(response, "/actions/publish/")

        self.client.force_login(self.outsider)
        hidden = self.client.get(reverse("dashboard:task-detail", args=[self.task.pk]))
        self.assertEqual(hidden.status_code, 404)

    def test_task_detail_core_controls_switch_fully_to_english(self):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        self.client.force_login(self.owner)
        response = self.client.get(reverse("dashboard:task-detail", args=[self.task.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Task details")
        self.assertContains(response, "Confirm each item: is this task ready to start?")
        self.assertContains(response, "Save readiness check")
        self.assertContains(response, "Select a result")
        self.assertNotContains(response, "逐项确认")
        self.assertNotContains(response, "保存开工检查结果")
        self.assertNotContains(response, "请选择结果")

    def test_draft_cancel_hides_task_from_today_but_preserves_audited_record(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "cancel"]),
            self._command_data(
                self.task,
                reason="Wrong product context selected during dogfood.",
                confirm="on",
            ),
        )

        self.assertRedirects(response, reverse("dashboard:home"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.CANCELLED)
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())
        event = self.task.state_events.get(to_state=Task.State.CANCELLED)
        self.assertEqual(event.from_state, Task.State.DRAFT)
        self.assertEqual(event.permission_grant_id, self.owner_cancel_grant.pk)
        self.assertEqual(event.reason, "Wrong product context selected during dogfood.")

        today = self.client.get(reverse("dashboard:home"))
        self.assertNotIn(self.task.pk, {task.pk for task in today.context["tasks"]})
        self.assertNotContains(today, self.task.title)

    def test_creator_without_edit_permission_cannot_record_dor(self):
        unauthorized_task = self._new_task(self.outsider)
        self.client.force_login(self.outsider)
        response = self.client.post(
            reverse("dashboard:task-action", args=[unauthorized_task.pk, "dor"]),
            self._command_data(
                unauthorized_task,
                criterion__source_ready=TaskCheckRun.Result.PASS,
            ),
        )
        self.assertEqual(response.status_code, 403)
        unauthorized_task.refresh_from_db()
        self.assertEqual(unauthorized_task.current_state, Task.State.DRAFT)
        self.assertEqual(unauthorized_task.state_version, 0)
        self.assertFalse(unauthorized_task.check_runs.exists())
        self.assertFalse(unauthorized_task.state_events.exists())

    def test_blocked_dor_is_preserved_before_a_new_pass_can_make_task_ready(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "dor"]),
            self._command_data(self.task, criterion__source_ready=TaskCheckRun.Result.BLOCKED),
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.BLOCKED)
        self.assertEqual(self.task.blocked_from_state, Task.State.DRAFT)
        first = self.task.check_runs.get(check_kind=TaskCheckRun.Kind.DOR)
        self.assertEqual(first.aggregate_result, TaskCheckRun.Result.BLOCKED)

        self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "resume"]),
            self._command_data(self.task),
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.DRAFT)
        self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "dor"]),
            self._command_data(self.task, criterion__source_ready=TaskCheckRun.Result.PASS),
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.READY)
        runs = list(self.task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOR).order_by("attempt_number"))
        self.assertEqual([run.aggregate_result for run in runs], [TaskCheckRun.Result.BLOCKED, TaskCheckRun.Result.PASS])

    def test_ready_assignment_is_explicit_and_only_assignee_can_start(self):
        self._pass_dor()
        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "assign"]),
            self._command_data(self.task, assignee=str(self.operator.pk)),
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.ASSIGNED)
        self.assertEqual(self.task.current_assignee_principal_id, self.operator.pk)
        self.assertEqual(self.task.assignments.get().assignee_principal_id, self.operator.pk)

        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "start"]),
            self._command_data(self.task),
        )
        self.assertEqual(response.status_code, 403)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.ASSIGNED)

        self.client.force_login(self.operator)
        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "start"]),
            self._command_data(self.task),
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.IN_PROGRESS)

    def test_delivery_link_seals_exact_asset_and_moves_only_to_under_review(self):
        self._assign_and_start()
        external_url = "https://docs.example.com/deliveries/answer-v1"
        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            self._command_data(
                self.task,
                external_url=external_url,
                submission_note="Ready for human review.",
                criterion__plain_language=TaskCheckRun.Result.PASS,
            ),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        version = ContentAssetVersion.objects.get(content_asset__task=self.task)
        encoded_url = external_url.encode("utf-8")
        self.assertEqual(version.object_key, external_url)
        self.assertEqual(version.mime_type, "text/uri-list")
        self.assertEqual(version.byte_size, len(encoded_url))
        self.assertEqual(version.content_sha256, hashlib.sha256(encoded_url).hexdigest())
        self.assertEqual(version.metadata, {"source": "external-url"})

        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.UNDER_REVIEW)
        self.assertEqual(self.task.state_version, 5)
        submission = TaskSubmission.objects.get(task=self.task)
        self.assertEqual(submission.primary_asset_version_id, version.pk)
        dod = self.task.check_runs.get(check_kind=TaskCheckRun.Kind.DOD)
        self.assertEqual(dod.aggregate_result, TaskCheckRun.Result.PASS)
        self.assertEqual(
            list(self.task.state_events.values_list("to_state", flat=True)),
            [
                Task.State.READY,
                Task.State.ASSIGNED,
                Task.State.IN_PROGRESS,
                Task.State.SUBMITTED,
                Task.State.UNDER_REVIEW,
            ],
        )
        self.assertContains(response, "交付内容已封存并送入人工审核")
        self.assertNotContains(response, "/actions/review/")
        self.assertNotContains(response, "/actions/gate/")
        self.assertNotContains(response, "/actions/publish/")

    def test_delivery_domain_failure_rolls_back_all_database_facts(self):
        self._assign_and_start()
        with patch(
            "dashboard.views.TaskSubmission.seal",
            side_effect=ValidationError("Simulated domain failure."),
        ):
            response = self.client.post(
                reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
                self._command_data(
                    self.task,
                    external_url="https://docs.example.com/deliveries/rollback",
                    submission_note="Must not survive.",
                    criterion__plain_language=TaskCheckRun.Result.PASS,
                ),
                follow=True,
            )
        self.assertEqual(response.status_code, 200)

        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.IN_PROGRESS)
        self.assertEqual(self.task.state_version, 3)
        self.assertFalse(ContentAsset.objects.filter(task=self.task).exists())
        self.assertFalse(ContentAssetVersion.objects.filter(content_asset__task=self.task).exists())
        self.assertFalse(self.task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).exists())
        self.assertFalse(TaskSubmission.objects.filter(task=self.task).exists())
        self.assertEqual(self.task.state_events.count(), 3)
        self.assertContains(response, "Simulated domain failure")

    def test_delivery_form_requires_external_url_and_has_no_file_input(self):
        self._assign_and_start()
        detail = self.client.get(reverse("dashboard:task-detail", args=[self.task.pk]))
        self.assertNotContains(detail, 'type="file"')
        self.assertNotContains(detail, "multipart/form-data")
        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            self._command_data(
                self.task,
                submission_note="Missing link must fail.",
                criterion__plain_language=TaskCheckRun.Result.PASS,
            ),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ContentAssetVersion.objects.filter(content_asset__task=self.task).exists())
        self.assertContains(response, "本次交付链接", status_code=400)

    def test_exact_delivery_command_replay_is_idempotent_and_conflicting_url_is_rejected(self):
        self._assign_and_start()
        root_command = uuid.uuid4()
        expected_version = self.task.state_version
        external_url = "https://docs.example.com/deliveries/exact-retry"

        def delivery_data(url):
            return {
                "command_id": str(root_command),
                "expected_state_version": expected_version,
                "external_url": url,
                "submission_note": "Exact retry contract.",
                "criterion__plain_language": TaskCheckRun.Result.PASS,
            }

        first = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            delivery_data(external_url),
            follow=True,
        )
        self.assertEqual(first.status_code, 200)

        replay = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            delivery_data(external_url),
            follow=True,
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(ContentAsset.objects.filter(task=self.task).count(), 1)
        self.assertEqual(ContentAssetVersion.objects.filter(content_asset__task=self.task).count(), 1)
        self.assertEqual(TaskSubmission.objects.filter(task=self.task).count(), 1)
        self.assertEqual(self.task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).count(), 1)

        conflict = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            delivery_data("https://docs.example.com/deliveries/conflict"),
            follow=True,
        )
        self.assertEqual(conflict.status_code, 200)
        self.assertContains(conflict, "command_id 已用于另一份交付内容或表单")
        self.assertEqual(ContentAssetVersion.objects.filter(content_asset__task=self.task).count(), 1)
        self.assertEqual(TaskSubmission.objects.filter(task=self.task).count(), 1)

    def test_human_rework_resumes_and_creates_new_version_dod_and_submission(self):
        self._assign_and_start()
        first_url = "https://docs.example.com/deliveries/rework-v1"
        second_url = "https://docs.example.com/deliveries/rework-v2"
        first_response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            self._command_data(
                self.task,
                external_url=first_url,
                submission_note="First submission.",
                criterion__plain_language=TaskCheckRun.Result.PASS,
            ),
        )
        self.assertEqual(first_response.status_code, 302)
        self.task.refresh_from_db()
        first_submission = TaskSubmission.objects.get(task=self.task)
        first_version = first_submission.primary_asset_version
        first_dod_id = first_submission.dod_check_run_id
        original_manifest_hash = first_submission.manifest_sha256

        review_grant = self._grant(self.owner, PermissionGrant.Action.REVIEW)
        review = ReviewDecision.record_final(
            submission=first_submission,
            decision=ReviewDecision.Decision.CHANGES_REQUESTED,
            rationale="Please revise the wording.",
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            reviewer_principal=self.owner,
            acting_role=self.owner.role,
            permission_grant=review_grant,
            recorded_by_principal=self.owner,
        )
        Task.transition(
            task_id=self.task.pk,
            to_state=Task.State.HUMAN_REWORK,
            command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version,
            actor_principal=self.owner,
            acting_role=self.owner.role,
            permission_grant=self.owner_grant,
            recorded_by_principal=self.owner,
            reason="Human review requested changes.",
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.HUMAN_REWORK)

        self.client.force_login(self.operator)
        detail = self.client.get(reverse("dashboard:task-detail", args=[self.task.pk]))
        self.assertContains(detail, "审核要求修改，恢复制作新版本")
        self.assertContains(detail, "恢复制作")
        resumed = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "resume-work"]),
            self._command_data(self.task),
        )
        self.assertEqual(resumed.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.IN_PROGRESS)

        second_response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            self._command_data(
                self.task,
                external_url=second_url,
                submission_note="Revised after human feedback.",
                criterion__plain_language=TaskCheckRun.Result.PASS,
            ),
            follow=True,
        )
        self.assertEqual(second_response.status_code, 200)

        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.UNDER_REVIEW)
        submissions = list(TaskSubmission.objects.filter(task=self.task).order_by("submission_number"))
        self.assertEqual(len(submissions), 2)
        second_submission = submissions[1]
        self.assertEqual(second_submission.supersedes_submission_id, first_submission.pk)
        self.assertEqual(second_submission.triggering_review_id, review.pk)
        self.assertEqual(second_submission.submission_number, 2)
        self.assertNotEqual(second_submission.dod_check_run_id, first_dod_id)
        self.assertEqual(second_submission.primary_asset_version.content_asset_id, first_version.content_asset_id)
        self.assertEqual(second_submission.primary_asset_version.version_number, 2)
        self.assertEqual(second_submission.primary_asset_version.object_key, second_url)
        self.assertEqual(
            second_submission.primary_asset_version.content_sha256,
            hashlib.sha256(second_url.encode("utf-8")).hexdigest(),
        )
        first_submission.refresh_from_db()
        self.assertEqual(first_submission.primary_asset_version_id, first_version.pk)
        self.assertEqual(first_submission.dod_check_run_id, first_dod_id)
        self.assertEqual(first_submission.manifest_sha256, original_manifest_hash)
        self.assertEqual(ContentAsset.objects.filter(task=self.task).count(), 1)
        self.assertEqual(ContentAssetVersion.objects.filter(content_asset__task=self.task).count(), 2)
        self.assertEqual(self.task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).count(), 2)

    def _inline_content_version(self, *, body="Complete platform-ready content v1"):
        asset = self.task.content_assets.filter(asset_key="publishable-content").first()
        if asset is None:
            asset = ContentAsset.create_idempotent(
                task=self.task,
                asset_key="publishable-content",
                title="Complete publishable content",
                asset_kind=ContentAsset.AssetKind.COPY,
                command_id=uuid.uuid4(),
                actor_principal=self.operator,
                acting_role=self.operator.role,
                permission_grant=self.operator_grant,
                recorded_by_principal=self.operator,
            )
        return ContentAssetVersion.create_next(
            content_asset=asset,
            representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
            inline_content=body,
            mime_type="text/plain; charset=utf-8",
            metadata={"source": "generated-inline-content", "title": "Ready copy"},
            command_id=uuid.uuid4(),
            actor_principal=self.operator,
            acting_role=self.operator.role,
            permission_grant=self.operator_grant,
            recorded_by_principal=self.operator,
        )

    @patch("dashboard.views.generate_task_content_draft")
    def test_current_assignee_can_request_offline_content_generation(self, generate):
        self._assign_and_start()
        generate.return_value = SimpleNamespace(created=True)

        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "generate-content"]),
            self._command_data(self.task),
        )

        self.assertRedirects(response, reverse("dashboard:task-detail", args=[self.task.pk]))
        call = generate.call_args.kwargs
        self.assertEqual(call["task"].pk, self.task.pk)
        self.assertEqual(call["principal"].pk, self.operator.pk)
        self.assertEqual(call["permission_grant"].pk, self.operator_grant.pk)
        self.assertNotIn("provider", call)

    @patch("dashboard.views.validate_inline_content_evidence_manifest")
    def test_inline_content_is_visible_editable_and_sealed_as_exact_submission(self, validate_manifest):
        self._assign_and_start()
        version = self._inline_content_version(body="Hook\n\nBody\n\nCTA")

        detail = self.client.get(reverse("dashboard:task-detail", args=[self.task.pk]))
        self.assertContains(detail, "Hook")
        self.assertContains(detail, "复制完整内容")
        self.assertContains(detail, "另存为新版本")
        self.assertContains(detail, "送审系统内的完整内容")

        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            self._command_data(
                self.task,
                delivery_mode="SYSTEM_CONTENT",
                content_version=str(version.pk),
                submission_note="Exact inline copy is ready.",
                criterion__plain_language=TaskCheckRun.Result.PASS,
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.UNDER_REVIEW)
        submission = self.task.submissions.get()
        self.assertEqual(submission.primary_asset_version_id, version.pk)
        self.assertEqual(version.content_asset.versions.count(), 1)
        validate_manifest.assert_called_once_with(asset_version=version, lock=True)

    @patch(
        "dashboard.views.validate_inline_content_evidence_manifest",
        side_effect=ValidationError(
            "该内容版本引用的外部需求证据已经作废、过期或发生变化，请重新生成内容后再继续。"
        ),
    )
    def test_inline_content_with_stale_evidence_cannot_be_submitted(self, validate_manifest):
        self._assign_and_start()
        version = self._inline_content_version(body="Content grounded in evidence that is now invalid")

        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            self._command_data(
                self.task,
                delivery_mode="SYSTEM_CONTENT",
                content_version=str(version.pk),
                submission_note="This must fail before sealing.",
                criterion__plain_language=TaskCheckRun.Result.PASS,
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "外部需求证据已经作废")
        validate_manifest.assert_called_once_with(asset_version=version, lock=True)
        self.assertFalse(TaskSubmission.objects.filter(task=self.task).exists())
        self.assertFalse(self.task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.IN_PROGRESS)

    def test_stale_inline_submission_is_rejected_without_partial_facts(self):
        self._assign_and_start()
        version = self._inline_content_version(body="Current complete content")

        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            self._command_data(
                self.task,
                expected_state_version=self.task.state_version - 1,
                delivery_mode="SYSTEM_CONTENT",
                content_version=str(version.pk),
                submission_note="This browser tab is stale.",
                criterion__plain_language=TaskCheckRun.Result.PASS,
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "任务状态已经变化")
        self.assertFalse(TaskSubmission.objects.filter(task=self.task).exists())
        self.assertFalse(self.task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.IN_PROGRESS)

    @patch("dashboard.views.revise_task_content_draft")
    def test_edit_action_passes_latest_exact_version_to_immutable_revision_service(self, revise):
        self._assign_and_start()
        version = self._inline_content_version(body="Original complete content")
        revise.return_value = SimpleNamespace(
            asset_version=SimpleNamespace(version_number=2),
            created=True,
        )

        response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "revise-content"]),
            self._command_data(
                self.task,
                source_version=str(version.pk),
                inline_content="Human-edited complete content",
            ),
        )

        self.assertRedirects(response, reverse("dashboard:task-detail", args=[self.task.pk]))
        call = revise.call_args.kwargs
        self.assertEqual(call["source_version"].pk, version.pk)
        self.assertEqual(call["inline_content"], "Human-edited complete content")
        self.assertEqual(call["permission_grant"].pk, self.operator_grant.pk)

    def test_operator_can_withdraw_unreviewed_submission_and_resubmit_as_v2(self):
        self._assign_and_start()
        first_url = "https://docs.example.com/deliveries/withdraw-v1"
        second_url = "https://docs.example.com/deliveries/withdraw-v2"

        first_response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            self._command_data(
                self.task,
                external_url=first_url,
                submission_note="Submitted with the wrong final wording.",
                criterion__plain_language=TaskCheckRun.Result.PASS,
            ),
        )
        self.assertEqual(first_response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.UNDER_REVIEW)
        first_submission = self.task.submissions.get()

        withdraw_response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "withdraw"]),
            self._command_data(
                self.task,
                reason="I noticed the final sentence was incorrect.",
                confirm="on",
            ),
        )
        self.assertRedirects(
            withdraw_response,
            reverse("dashboard:task-detail", args=[self.task.pk]),
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.IN_PROGRESS)
        withdrawal = self.task.state_events.get(
            event_type=TaskStateEvent.EventType.SUBMISSION_WITHDRAWN
        )
        self.assertEqual(withdrawal.submission_id, first_submission.pk)
        self.assertEqual(withdrawal.permission_grant_id, self.operator_grant.pk)
        self.assertFalse(ReviewDecision.objects.filter(submission=first_submission).exists())

        second_response = self.client.post(
            reverse("dashboard:task-action", args=[self.task.pk, "deliver"]),
            self._command_data(
                self.task,
                external_url=second_url,
                submission_note="Corrected before reviewer decision.",
                criterion__plain_language=TaskCheckRun.Result.PASS,
            ),
            follow=True,
        )
        self.assertEqual(second_response.status_code, 200)

        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.UNDER_REVIEW)
        submissions = list(self.task.submissions.order_by("submission_number"))
        self.assertEqual(len(submissions), 2)
        self.assertEqual(submissions[1].supersedes_submission_id, first_submission.pk)
        self.assertIsNone(submissions[1].triggering_review_id)
        self.assertEqual(submissions[1].primary_asset_version.version_number, 2)
        self.assertEqual(submissions[1].primary_asset_version.object_key, second_url)
        self.assertEqual(
            submissions[1].primary_asset_version.content_sha256,
            hashlib.sha256(second_url.encode("utf-8")).hexdigest(),
        )

    def _new_task_form_data(self, *, title="Draft a new evidence-led answer", description="Explain why this task matters."):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("dashboard:task-create"))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        return {
            "task_id": str(form["task_id"].value()),
            "command_id": str(form["command_id"].value()),
            "task_token": form["task_token"].value(),
            "product_profile_version": str(self.profile.pk),
            "contract_version": str(self.contract.pk),
            "title": title,
            "description": description,
        }

    def test_authorized_admin_creates_exact_unassigned_draft_task(self):
        self.client.force_login(self.owner)
        home = self.client.get(reverse("dashboard:home"))
        self.assertContains(home, "新建任务")
        data = self._new_task_form_data()
        generated_id = uuid.UUID(data["task_id"])
        self.assertEqual(generated_id.version, 7)

        response = self.client.post(reverse("dashboard:task-create"), data)
        self.assertEqual(response.status_code, 302)
        created = Task.objects.get(pk=generated_id)
        self.assertEqual(created.product_id, self.product.pk)
        self.assertEqual(created.product_profile_version_id, self.profile.pk)
        self.assertEqual(created.contract_version_id, self.contract.pk)
        self.assertEqual(created.current_state, Task.State.DRAFT)
        self.assertEqual(created.state_version, 0)
        self.assertIsNone(created.current_assignee_principal_id)
        self.assertEqual(created.created_by_principal_id, self.owner.pk)
        self.assertEqual(created.creation_command_id, uuid.UUID(data["command_id"]))
        self.assertEqual(created.created_under_grant_id, self.owner_create_grant.pk)
        self.assertEqual(created.created_by_acting_role, self.owner.role)
        self.assertEqual(len(created.creation_payload_hash), 64)
        self.assertEqual(created.updated_by_principal_id, self.owner.pk)
        self.assertRedirects(response, reverse("dashboard:task-detail", args=[created.pk]))

    def test_operator_with_edit_grant_cannot_see_or_use_task_creation(self):
        self.client.force_login(self.operator)
        home = self.client.get(reverse("dashboard:home"))
        self.assertNotContains(home, "新建任务")
        response = self.client.get(reverse("dashboard:task-create"))
        self.assertEqual(response.status_code, 403)

    def test_revoked_create_task_grant_fails_closed_at_post_time(self):
        data = self._new_task_form_data()
        generated_id = uuid.UUID(data["task_id"])
        self.owner_create_grant.grant_status = PermissionGrant.GrantStatus.REVOKED
        self.owner_create_grant.revoked_at = timezone.now()
        self.owner_create_grant.revoked_by_principal = self.owner
        self.owner_create_grant.revocation_reason = "Test revocation before submit."
        self.owner_create_grant.save(
            update_fields=[
                "grant_status",
                "revoked_at",
                "revoked_by_principal",
                "revocation_reason",
                "updated_at",
            ]
        )

        response = self.client.post(reverse("dashboard:task-create"), data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Task.objects.filter(pk=generated_id).exists())

    def test_creation_excludes_historical_profile_and_non_latest_contract(self):
        second_profile = ProductProfileVersion.objects.create(
            product=self.product,
            version_number=2,
            market_code="US",
            language_code="en",
            audience={"need": "second profile"},
            core_value_proposition="Second sealed configuration.",
            brand_voice={"tone": "clear"},
            product_facts={},
            prohibited_expressions=[],
            created_by_principal=self.owner,
        )
        second_profile.seal(self.owner)
        second_contract = TaskContractVersion.objects.create(
            product_profile_version=second_profile,
            version_number=1,
            title="Second exact contract",
            dor_criteria=[{"key": "source_ready"}],
            dod_criteria=[{"key": "plain_language"}],
            release_gate_criteria=[{"key": "human_review"}],
            success_criteria=[{"key": "observed_result", "required": False}],
            sealed_at=timezone.now(),
            created_by_principal=self.owner,
        )
        latest_contract = TaskContractVersion.objects.create(
            product_profile_version=second_profile,
            version_number=2,
            title="Current exact contract",
            dor_criteria=[{"key": "source_ready"}],
            dod_criteria=[{"key": "plain_language"}],
            release_gate_criteria=[{"key": "human_review"}],
            success_criteria=[{"key": "observed_result", "required": False}],
            sealed_at=timezone.now(),
            created_by_principal=self.owner,
        )
        self.product.current_profile_version = second_profile
        self.product.save(update_fields=["current_profile_version", "updated_at"])

        self.client.force_login(self.owner)
        page = self.client.get(reverse("dashboard:task-create"))
        profile_ids = set(
            page.context["form"].fields["product_profile_version"].queryset.values_list("pk", flat=True)
        )
        contract_ids = set(
            page.context["form"].fields["contract_version"].queryset.values_list("pk", flat=True)
        )
        self.assertEqual(profile_ids, {second_profile.pk})
        self.assertEqual(contract_ids, {latest_contract.pk})

        form = page.context["form"]
        base = {
            "task_id": str(form["task_id"].value()),
            "command_id": str(form["command_id"].value()),
            "task_token": form["task_token"].value(),
            "title": "Historical inputs must be rejected",
            "description": "Fail closed.",
        }
        historical = self.client.post(
            reverse("dashboard:task-create"),
            {
                **base,
                "product_profile_version": str(self.profile.pk),
                "contract_version": str(self.contract.pk),
            },
        )
        self.assertEqual(historical.status_code, 400)
        self.assertFalse(Task.objects.filter(pk=base["task_id"]).exists())

        old_contract = self.client.post(
            reverse("dashboard:task-create"),
            {
                **base,
                "product_profile_version": str(second_profile.pk),
                "contract_version": str(second_contract.pk),
            },
        )
        self.assertEqual(old_contract.status_code, 400)
        self.assertFalse(Task.objects.filter(pk=base["task_id"]).exists())

    def test_task_creation_exact_replay_returns_existing_and_changed_payload_conflicts(self):
        data = self._new_task_form_data()
        generated_id = uuid.UUID(data["task_id"])
        first = self.client.post(reverse("dashboard:task-create"), data)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(Task.objects.filter(pk=generated_id).count(), 1)

        replay = self.client.post(reverse("dashboard:task-create"), data, follow=True)
        self.assertEqual(replay.status_code, 200)
        self.assertContains(replay, "没有重复创建")
        self.assertEqual(Task.objects.filter(pk=generated_id).count(), 1)

        changed = {**data, "title": "A conflicting task title"}
        conflict = self.client.post(reverse("dashboard:task-create"), changed)
        self.assertEqual(conflict.status_code, 409)
        self.assertContains(conflict, "该任务 ID 已用于另一份创建内容", status_code=409)
        existing = Task.objects.get(pk=generated_id)
        self.assertEqual(existing.title, data["title"])
        self.assertEqual(Task.objects.filter(pk=generated_id).count(), 1)

    def test_task_creation_unique_race_reads_winner_and_applies_exact_payload_check(self):
        data = self._new_task_form_data()
        initial = self.client.post(reverse("dashboard:task-create"), data)
        self.assertEqual(initial.status_code, 302)
        winner = Task.objects.get(pk=data["task_id"])

        locked_queryset = MagicMock()
        locked_queryset.filter.return_value.first.side_effect = [None, winner]
        with (
            patch("dashboard.views.Task.objects.select_for_update", return_value=locked_queryset),
            patch("dashboard.views.Task.save", side_effect=IntegrityError("duplicate task id")),
        ):
            response = self.client.post(reverse("dashboard:task-create"), data, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "没有重复创建")
        self.assertEqual(Task.objects.filter(pk=winner.pk).count(), 1)
