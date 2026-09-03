import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from dailyops.models import DailyBatchDispositionEvent
from intelligence.models import canonical_sha256
from products.models import Product, ProductProfileVersion
from workflow.models import ActingRole, Task, TaskAssignment, TaskContractVersion


class MyTaskHistoryTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="history-owner",
            password="LocalPassword123!",
            display_name="History Owner",
            role=Principal.Role.OWNER,
        )
        self.user = Principal.objects.create_user(
            username="history-operator",
            password="LocalPassword123!",
            display_name="History Operator",
            role=Principal.Role.OPERATOR,
        )
        self.other = Principal.objects.create_user(
            username="history-other",
            password="LocalPassword123!",
            display_name="Other Operator",
            role=Principal.Role.OPERATOR,
        )
        self.product, self.profile, self.contract = self._make_product("VISIBLE")
        self.hidden_product, self.hidden_profile, self.hidden_contract = self._make_product(
            "HIDDEN"
        )
        self.view_grant = self._grant(
            self.user,
            self.product,
            PermissionGrant.Action.VIEW,
        )
        self.assign_grant = self._grant(
            self.owner,
            self.product,
            PermissionGrant.Action.ASSIGN_TASK,
        )
        self.cancel_grant = self._grant(
            self.owner,
            self.product,
            PermissionGrant.Action.CANCEL_TASK,
        )

    def _make_product(self, code):
        product = Product.objects.create(
            product_code=f"HISTORY-{code}",
            name=f"History {code}",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        profile = ProductProfileVersion.objects.create(
            product=product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"need": "history test"},
            core_value_proposition="History test product.",
            brand_voice={"tone": "clear"},
            product_facts={},
            prohibited_expressions=[],
            created_by_principal=self.owner,
        )
        profile.seal(self.owner)
        contract = TaskContractVersion.objects.create(
            product_profile_version=profile,
            version_number=1,
            title="History contract",
            dor_criteria=[{"key": "ready", "label": "Ready"}],
            dod_criteria=[{"key": "done", "label": "Done"}],
            release_gate_criteria=[{"key": "review", "label": "Reviewed"}],
            success_criteria=[{"key": "history_visible", "required": False}],
            sealed_at=timezone.now(),
            created_by_principal=self.owner,
        )
        return product, profile, contract

    def _grant(
        self,
        principal,
        product,
        action,
        *,
        valid=True,
        effect=PermissionGrant.Effect.ALLOW,
    ):
        now = timezone.now()
        return PermissionGrant.objects.create(
            principal=principal,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=product,
            action=action,
            effect=effect,
            valid_from=now - timedelta(hours=2),
            valid_until=(now + timedelta(days=1) if valid else now - timedelta(hours=1)),
            granted_by_principal=self.owner,
        )

    def _task(self, title, *, creator=None, hidden=False):
        creator = creator or self.owner
        product = self.hidden_product if hidden else self.product
        profile = self.hidden_profile if hidden else self.profile
        contract = self.hidden_contract if hidden else self.contract
        return Task.objects.create(
            product=product,
            product_profile_version=profile,
            contract_version=contract,
            title=title,
            description="Read-only history test.",
            created_by_principal=creator,
            updated_by_principal=creator,
        )

    def _assign_fact(self, task, assignee):
        return TaskAssignment.objects.create(
            task=task,
            assignee_principal=assignee,
            assignment_number=1,
            command_id=uuid.uuid4(),
            payload_hash="a" * 64,
            expected_task_version=0,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.assign_grant,
            recorded_by_principal=self.owner,
            assigned_at=timezone.now(),
        )

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("dashboard:my-task-history"))
        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('dashboard:my-task-history')}",
        )

    def test_history_entry_is_only_in_the_account_popover(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:home"))
        history_url = reverse("dashboard:my-task-history")
        self.assertContains(response, "我的工作记录")
        self.assertEqual(response.content.decode().count(f'href="{history_url}"'), 1)

    def test_history_is_current_user_only_deduplicated_and_view_filtered(self):
        created_and_assigned = self._task("Created and worked", creator=self.user)
        self._assign_fact(created_and_assigned, self.user)
        assigned_only = self._task("Past assignment", creator=self.owner)
        self._assign_fact(assigned_only, self.user)
        unrelated = self._task("Another account only", creator=self.other)
        hidden = self._task("No current view permission", creator=self.user, hidden=True)

        self.client.force_login(self.user)
        response = self.client.get(
            f"{reverse('dashboard:my-task-history')}?user_id={self.other.pk}"
        )

        visible_ids = [task.pk for task in response.context["page_obj"].object_list]
        self.assertEqual(set(visible_ids), {created_and_assigned.pk, assigned_only.pk})
        self.assertEqual(visible_ids.count(created_and_assigned.pk), 1)
        self.assertNotIn(unrelated.pk, visible_ids)
        self.assertNotIn(hidden.pk, visible_ids)
        self.assertContains(response, "Created and worked")
        self.assertContains(response, "Past assignment")
        self.assertNotContains(response, "Another account only")
        self.assertNotContains(response, "No current view permission")
        self.assertContains(
            response,
            reverse("dashboard:task-detail", args=[created_and_assigned.pk]),
        )
        created_task = next(
            task
            for task in response.context["page_obj"].object_list
            if task.pk == created_and_assigned.pk
        )
        self.assertEqual(
            [label["zh"] for label in created_task.participation_labels],
            ["创建", "执行"],
        )

    def test_history_categories_do_not_call_cancelled_or_hidden_work_completed(self):
        active = self._task("Still active", creator=self.user)
        completed = self._task("Actually completed", creator=self.user)
        cancelled = self._task("Cancelled before completion", creator=self.user)
        Task.objects.filter(pk=completed.pk).update(current_state=Task.State.DONE)
        Task.objects.filter(pk=cancelled.pk).update(current_state=Task.State.CANCELLED)
        # A Daily draft can be abandoned before it ever compiles a formal
        # Task.  Its personal history must still be discoverable from the
        # immutable disposition event alone.
        abandoned_product, _, _ = self._make_product("ABANDONED-NO-TASK")
        abandoned_cancel_grant = self._grant(
            self.owner,
            abandoned_product,
            PermissionGrant.Action.CANCEL_TASK,
        )
        self._grant(self.owner, abandoned_product, PermissionGrant.Action.VIEW)
        self.assertFalse(Task.objects.filter(product=abandoned_product).exists())
        hidden_batch = uuid.uuid4()
        disposition_payload = {
            "batch_key": str(hidden_batch),
            "product_id": str(abandoned_product.pk),
            "disposition": DailyBatchDispositionEvent.Disposition.ABANDONED,
            "reason": "Wrong draft; start again.",
        }
        DailyBatchDispositionEvent.objects.create(
            batch_key=hidden_batch,
            product=abandoned_product,
            disposition=DailyBatchDispositionEvent.Disposition.ABANDONED,
            reason="Wrong draft; start again.",
            principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=abandoned_cancel_grant,
            command_id=uuid.uuid4(),
            payload_hash=canonical_sha256(disposition_payload),
        )
        self.client.force_login(self.user)

        active_response = self.client.get(reverse("dashboard:my-task-history"))
        self.assertContains(active_response, active.title)
        self.assertNotContains(active_response, completed.title)
        self.assertNotContains(active_response, cancelled.title)

        completed_response = self.client.get(
            f"{reverse('dashboard:my-task-history')}?category=completed"
        )
        self.assertContains(completed_response, completed.title)
        self.assertNotContains(completed_response, cancelled.title)

        cancelled_response = self.client.get(
            f"{reverse('dashboard:my-task-history')}?category=cancelled"
        )
        self.assertContains(cancelled_response, cancelled.title)
        self.assertNotContains(cancelled_response, completed.title)

        self.client.force_login(self.owner)
        hidden_response = self.client.get(
            f"{reverse('dashboard:my-task-history')}?category=hidden"
        )
        self.assertContains(hidden_response, "草稿已删除/隐藏")
        self.assertContains(hidden_response, "它不是“已完成任务”")
        self.assertNotContains(hidden_response, completed.title)

    def test_participant_can_open_read_only_detail_but_view_grant_alone_cannot(self):
        task = self._task("History detail access", creator=self.user)
        self._grant(self.other, self.product, PermissionGrant.Action.VIEW)
        detail_url = reverse("dashboard:task-detail", args=[task.pk])

        self.client.force_login(self.other)
        denied = self.client.get(detail_url)
        self.assertEqual(denied.status_code, 404)

        self.client.force_login(self.user)
        allowed = self.client.get(detail_url)
        self.assertEqual(allowed.status_code, 200)
        self.assertContains(allowed, task.title)

        Task.objects.filter(pk=task.pk).update(current_state=Task.State.DONE)
        terminal = self.client.get(detail_url)
        self.assertEqual(terminal.status_code, 200)
        self.assertContains(terminal, "只能查看历史")

    def test_expired_view_grant_hides_the_summary(self):
        product, profile, contract = self._make_product("EXPIRED")
        self._grant(self.user, product, PermissionGrant.Action.VIEW, valid=False)
        task = Task.objects.create(
            product=product,
            product_profile_version=profile,
            contract_version=contract,
            title="Expired permission history",
            description="Must stay hidden.",
            created_by_principal=self.user,
            updated_by_principal=self.user,
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:my-task-history"))
        self.assertNotContains(response, task.title)

    def test_current_deny_view_grant_hides_the_summary(self):
        task = self._task("Denied permission history", creator=self.user)
        self._grant(
            self.user,
            self.product,
            PermissionGrant.Action.VIEW,
            effect=PermissionGrant.Effect.DENY,
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("dashboard:my-task-history"))

        self.assertNotContains(response, task.title)

    def test_history_is_paginated_twenty_per_page(self):
        tasks = [self._task(f"Paged task {number:02d}", creator=self.user) for number in range(21)]
        self.client.force_login(self.user)

        first = self.client.get(reverse("dashboard:my-task-history"))
        second = self.client.get(f"{reverse('dashboard:my-task-history')}?page=2")

        self.assertEqual(first.context["page_obj"].paginator.count, 21)
        self.assertEqual(len(first.context["page_obj"].object_list), 20)
        self.assertEqual(len(second.context["page_obj"].object_list), 1)
        self.assertEqual(second.context["page_obj"].object_list[0].pk, tasks[0].pk)
