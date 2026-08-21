from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from accounts.services import revoke_permission_grant
from dailyops.services import PLATFORMS, ensure_default_sources
from integrations.connectors.types import Platform
from intelligence.models import (
    ChannelPlan,
    ChannelPlanStateEvent,
    CollectionRun,
    EvidenceInvalidationEvent,
    ExternalEvidenceItem,
    Initiative,
    InitiativeStateEvent,
    OpportunityStateEvent,
    ProductOpportunity,
    SignalAssessment,
    SourceRegistry,
    TaskCompilationContext,
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
from workflow.models import Task, TaskCheckRun, TaskContractPolicyLink, TaskContractVersion


@override_settings(ROOT_URLCONF="dailyops.test_urls")
class DailyOperationsUITests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="daily-owner",
            password="safe-local-password-123",
            role=Principal.Role.OWNER,
        )
        self.outsider = Principal.objects.create_user(
            username="daily-outsider",
            password="safe-local-password-456",
            role=Principal.Role.OPERATOR,
        )
        self.product = Product.objects.create(
            product_code="PUKO-DAILY-UI",
            name="PUKO Daily UI",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        now = timezone.now()
        for action in (
            PermissionGrant.Action.VIEW,
            PermissionGrant.Action.EDIT,
            PermissionGrant.Action.CREATE_TASK,
        ):
            PermissionGrant.objects.create(
                principal=self.owner,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                product=self.product,
                action=action,
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(days=1),
                granted_by_principal=self.owner,
            )
        PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(days=1),
            granted_by_principal=self.owner,
        )
        PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            action=PermissionGrant.Action.MANAGE_ACCOUNT,
            risk_level=PermissionGrant.RiskLevel.HIGH,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(days=1),
            granted_by_principal=self.owner,
        )
        ensure_default_sources(principal=self.owner, acting_role=self.owner.role)
        self._configure_compiler_context()
        self.client.force_login(self.owner)

    def _configure_compiler_context(self):
        objective = ObjectiveProfileVersion.objects.create(
            objective_key="DAILY_UI",
            version_number=1,
            primary_objectives=["reach"],
            secondary_objectives=["engagement"],
            retained_metrics=["purchase"],
            priority_rules={"reach": 1},
            strategy_boundaries={"human_approval": True},
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
            audience={"intent": "focus"},
            core_value_proposition="Evidence-informed daily focus support.",
            brand_voice={"tone": "clear"},
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
        contract = TaskContractVersion.objects.create(
            product_profile_version=profile,
            version_number=1,
            title="Daily link-first task",
            dor_criteria=[{"key": "source_ready", "required": True}],
            dod_criteria=[{"key": "external_link", "required": True}],
            release_gate_criteria=[{"key": "policy_pass", "required": True}],
            success_criteria=[{"key": "published_url", "required": True}],
            sealed_at=timezone.now(),
            created_by_principal=self.owner,
        )
        definition = PolicyDefinition.objects.create(
            policy_code="DAILY-UI-SAFE",
            name="Daily UI safety",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        policy = PolicyVersion.objects.create(
            policy_definition=definition,
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
        self.account = ChannelAccount.objects.create(
            platform_code="TIKTOK",
            account_code="daily-ui-tiktok",
            external_account_ref="@puko-daily-ui",
            display_name="PUKO Daily TikTok",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.environment = RuntimeEnvironment.objects.create(
            environment_code="daily-ui-staging",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="daily-ui",
            database_namespace="daily-ui",
            object_storage_namespace="link-only",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.binding = AccountEnvironmentBinding.objects.create(
            channel_account=self.account,
            runtime_environment=self.environment,
            binding_version=1,
            identity_reference="daily-ui-browser-profile",
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.capability = CapabilityState.objects.create(
            account_environment_binding=self.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=1,
            state=CapabilityState.State.OPEN,
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )

    def _start(self, batch_key=None):
        batch_key = batch_key or uuid.uuid4()
        start = timezone.now() - timedelta(days=7)
        end = timezone.now()
        data = {
            "command_id": str(batch_key),
            "product": str(self.product.pk),
            "query": "afternoon focus",
            "window_start": timezone.localtime(start).strftime("%Y-%m-%dT%H:%M"),
            "window_end": timezone.localtime(end).strftime("%Y-%m-%dT%H:%M"),
        }
        response = self.client.post(reverse("dailyops:batch-start"), data)
        return batch_key, data, response

    def _transition(self, name, object_id, state, version, reason):
        return self.client.post(
            reverse(f"dailyops:{name}", args=[object_id]),
            {
                "command_id": str(uuid.uuid4()),
                "expected_version": version,
                "to_state": state,
                "reason": reason,
            },
        )

    def _pending_proposal(self):
        batch_key, _, _ = self._start()
        self.client.post(
            reverse(
                "dailyops:evidence-manual",
                args=[self.product.pk, batch_key, Platform.TIKTOK.value],
            ),
            {
                "command_id": str(uuid.uuid4()),
                "collected_at": timezone.now().isoformat(),
                "external_url": "https://www.tiktok.com/@example/video/owner-shortcut",
                "external_content_id": "owner-shortcut",
                "title": "Owner shortcut evidence",
                "content_text": "A real provenance-linked input for the shortcut test.",
            },
        )
        self.client.post(
            reverse("dailyops:analysis-propose", args=[self.product.pk, batch_key]),
            {"command_id": str(uuid.uuid4())},
        )
        return batch_key, SignalAssessment.objects.get(
            assessment_key=f"daily-analysis-{batch_key.hex}",
            version_number=1,
        )

    def _approved_initiative(self):
        """Create the smallest human-approved planning context through the UI."""

        batch_key, _, _ = self._start()
        self.client.post(
            reverse(
                "dailyops:evidence-manual",
                args=[self.product.pk, batch_key, Platform.TIKTOK.value],
            ),
            {
                "command_id": str(uuid.uuid4()),
                "collected_at": timezone.now().isoformat(),
                "external_url": "https://www.tiktok.com/@example/video/channel-plan-test",
                "external_content_id": "channel-plan-test",
                "title": "Channel plan test evidence",
                "content_text": "A real provenance-linked input for an offline test.",
            },
        )
        self.client.post(
            reverse("dailyops:analysis-propose", args=[self.product.pk, batch_key]),
            {"command_id": str(uuid.uuid4())},
        )
        proposal = SignalAssessment.objects.get(
            assessment_key=f"daily-analysis-{batch_key.hex}",
            version_number=1,
        )
        self.client.post(
            reverse(
                "dailyops:analysis-accept",
                args=[self.product.pk, batch_key, proposal.pk],
            ),
            {"command_id": str(uuid.uuid4())},
        )
        opportunity = ProductOpportunity.objects.get(opportunity_key=f"daily-{batch_key.hex}")
        self._transition(
            "opportunity-transition",
            opportunity.pk,
            ProductOpportunity.State.TRIAGED,
            0,
            "人工初审。",
        )
        opportunity.refresh_from_db()
        self._transition(
            "opportunity-transition",
            opportunity.pk,
            ProductOpportunity.State.APPROVED,
            1,
            "人工批准进入计划。",
        )
        self.client.post(
            reverse("dailyops:initiative-create", args=[opportunity.pk]),
            {"command_id": str(uuid.uuid4())},
        )
        initiative = Initiative.objects.get(opportunity=opportunity)
        self._transition(
            "initiative-transition",
            initiative.pk,
            Initiative.State.APPROVED,
            0,
            "批准执行。",
        )
        initiative.refresh_from_db()
        return batch_key, initiative

    def _plan_data(self, **overrides):
        data = {
            "command_id": str(uuid.uuid4()),
            "platform": Platform.TIKTOK.value,
            "plan_date": timezone.localdate().isoformat(),
            "task_title": "回答下午注意力问题",
            "task_description": "根据已确认证据制作一条内容，并保存外部链接。",
        }
        data.update(overrides)
        return data

    def test_source_setup_is_offline_idempotent_and_requires_global_management(self):
        response = self.client.post(reverse("dailyops:source-setup"))
        self.assertRedirects(response, reverse("dailyops:home"))
        self.assertEqual(SourceRegistry.objects.filter(source_key__startswith="daily-").count(), 28)
        self.assertFalse(CollectionRun.objects.exists())

        self.client.force_login(self.outsider)
        denied = self.client.post(reverse("dailyops:source-setup"))
        self.assertEqual(denied.status_code, 403)

    def test_channel_plan_derives_account_environment_and_capability(self):
        batch_key, initiative = self._approved_initiative()
        detail = self.client.get(
            reverse("dailyops:batch-detail", args=[self.product.pk, batch_key])
        )
        self.assertNotContains(detail, 'name="environment_code"')
        self.assertNotContains(detail, 'name="capability_code"')

        response = self.client.post(
            reverse("dailyops:plan-create", args=[initiative.pk]),
            self._plan_data(),  # no account/environment/capability user input
            follow=True,
        )
        self.assertContains(response, "已建立 TIKTOK 平台任务安排")
        plan = ChannelPlan.objects.get(initiative=initiative)
        self.assertEqual(plan.channel_account_id, self.account.pk)
        self.assertEqual(
            plan.content_requirements["runtime_environment_code"],
            self.environment.environment_code,
        )
        self.assertEqual(
            plan.content_requirements["capability_state_id"],
            str(self.capability.pk),
        )
        self.assertNotIn("environment_code", plan.content_requirements)
        self.assertNotIn("capability_code", plan.content_requirements)

    def test_channel_plan_fails_closed_without_current_binding(self):
        batch_key, initiative = self._approved_initiative()
        ChannelAccount.objects.create(
            platform_code=Platform.PINTEREST.value,
            account_code="daily-ui-pinterest-unbound",
            external_account_ref="puko-unbound",
            display_name="PUKO Pinterest without environment",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        response = self.client.post(
            reverse("dailyops:plan-create", args=[initiative.pk]),
            self._plan_data(platform=Platform.PINTEREST.value),
            follow=True,
        )
        self.assertContains(response, "当前没有可用的运行环境")
        self.assertNotContains(response, "__all__")
        self.assertFalse(ChannelPlan.objects.filter(initiative=initiative).exists())

    def test_channel_plan_fails_closed_with_multiple_current_bindings(self):
        _, initiative = self._approved_initiative()
        second_environment = RuntimeEnvironment.objects.create(
            environment_code="daily-ui-second-staging",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="daily-ui-second",
            database_namespace="daily-ui-second",
            object_storage_namespace="link-only",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        AccountEnvironmentBinding.objects.create(
            channel_account=self.account,
            runtime_environment=second_environment,
            binding_version=1,
            identity_reference="daily-ui-second-browser-profile",
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        response = self.client.post(
            reverse("dailyops:plan-create", args=[initiative.pk]),
            self._plan_data(),
            follow=True,
        )
        self.assertContains(response, "同时连接了多个运行环境")
        self.assertFalse(ChannelPlan.objects.filter(initiative=initiative).exists())

    def test_channel_plan_fails_closed_when_current_capability_is_closed(self):
        _, initiative = self._approved_initiative()
        CapabilityState.objects.create(
            account_environment_binding=self.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=2,
            state=CapabilityState.State.CLOSED,
            effective_from=timezone.now() - timedelta(minutes=1),
            reason="Negative setup test.",
            supersedes=self.capability,
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        response = self.client.post(
            reverse("dailyops:plan-create", args=[initiative.pk]),
            self._plan_data(),
            follow=True,
        )
        self.assertContains(response, "当前不能人工发布")
        self.assertFalse(ChannelPlan.objects.filter(initiative=initiative).exists())

    def test_channel_plan_rejects_cross_platform_account_without_all_label(self):
        _, initiative = self._approved_initiative()
        pinterest_account = ChannelAccount.objects.create(
            platform_code=Platform.PINTEREST.value,
            account_code="daily-ui-pinterest",
            external_account_ref="puko-pinterest",
            display_name="PUKO Pinterest",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        response = self.client.post(
            reverse("dailyops:plan-create", args=[initiative.pk]),
            self._plan_data(channel_account=str(pinterest_account.pk)),
            follow=True,
        )
        self.assertContains(response, "所选账号与执行平台不匹配")
        self.assertNotContains(response, "__all__")
        self.assertFalse(ChannelPlan.objects.filter(initiative=initiative).exists())

    def test_offline_human_confirmed_flow_compiles_one_real_task(self):
        batch_key, start_data, response = self._start()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CollectionRun.objects.filter(batch_key=batch_key).count(), 7)
        self.assertEqual(
            set(
                CollectionRun.objects.filter(batch_key=batch_key).values_list(
                    "source__platform_code", flat=True
                )
            ),
            {platform.value for platform in PLATFORMS},
        )

        # Same hidden command and payload is replay-safe: no second batch.
        replay = self.client.post(reverse("dailyops:batch-start"), start_data)
        self.assertEqual(replay.status_code, 302)
        self.assertEqual(CollectionRun.objects.filter(batch_key=batch_key).count(), 7)

        detail = self.client.get(
            reverse("dailyops:batch-detail", args=[self.product.pk, batch_key])
        )
        self.assertEqual(detail.status_code, 200)
        for display in (
            "Pinterest",
            "Quora",
            "TikTok",
            "Shopify / 独立站",
            "Google 搜索",
            "Google Search Console",
            "Google Analytics 4",
        ):
            self.assertContains(detail, display)

        manual_command = uuid.uuid4()
        manual_data = {
            "command_id": str(manual_command),
            "collected_at": timezone.now().isoformat(),
            "external_url": "https://www.tiktok.com/@example/video/123",
            "external_content_id": "video-123",
            "title": "People asking about afternoon focus",
            "content_text": "How do I stay focused during the afternoon slump?",
        }
        manual_url = reverse(
            "dailyops:evidence-manual",
            args=[self.product.pk, batch_key, "TIKTOK"],
        )
        self.assertEqual(self.client.post(manual_url, manual_data).status_code, 302)
        self.assertEqual(ExternalEvidenceItem.objects.filter(collection_run__batch_key=batch_key).count(), 1)
        self.assertEqual(self.product.opportunities.count(), 0)  # stays zero until human acceptance
        run_count = CollectionRun.objects.filter(batch_key=batch_key).count()
        self.assertEqual(self.client.post(manual_url, manual_data).status_code, 302)
        self.assertEqual(CollectionRun.objects.filter(batch_key=batch_key).count(), run_count)

        propose_url = reverse("dailyops:analysis-propose", args=[self.product.pk, batch_key])
        self.assertEqual(
            self.client.post(propose_url, {"command_id": str(uuid.uuid4())}).status_code,
            302,
        )
        proposal = SignalAssessment.objects.get(
            assessment_key=f"daily-analysis-{batch_key.hex}", version_number=1
        )
        self.assertEqual(proposal.method, "AI_PROPOSAL")
        self.assertIn("dry-run", proposal.model_reference)
        self.assertNotIn("deepseek", proposal.model_reference.lower())
        self.assertFalse(ProductOpportunity.objects.filter(product=self.product).exists())

        accept_url = reverse(
            "dailyops:analysis-accept",
            args=[self.product.pk, batch_key, proposal.pk],
        )
        self.assertEqual(
            self.client.post(accept_url, {"command_id": str(uuid.uuid4())}).status_code,
            302,
        )
        opportunity = ProductOpportunity.objects.get(opportunity_key=f"daily-{batch_key.hex}")
        self._transition("opportunity-transition", opportunity.pk, "TRIAGED", 0, "证据相关，进入初审。")
        opportunity.refresh_from_db()
        self._transition("opportunity-transition", opportunity.pk, "APPROVED", 1, "人工批准进入计划。")
        opportunity.refresh_from_db()
        self.assertEqual(opportunity.current_state, ProductOpportunity.State.APPROVED)

        create_initiative_url = reverse("dailyops:initiative-create", args=[opportunity.pk])
        command = uuid.uuid4()
        self.assertEqual(
            self.client.post(create_initiative_url, {"command_id": str(command)}).status_code,
            302,
        )
        self.client.post(create_initiative_url, {"command_id": str(command)})
        self.assertEqual(Initiative.objects.filter(opportunity=opportunity).count(), 1)
        initiative = Initiative.objects.get(opportunity=opportunity)
        self._transition("initiative-transition", initiative.pk, "APPROVED", 0, "批准执行。")
        initiative.refresh_from_db()

        plan_command = uuid.uuid4()
        plan_url = reverse("dailyops:plan-create", args=[initiative.pk])
        plan_data = {
            "command_id": str(plan_command),
            "platform": "TIKTOK",
            "plan_date": timezone.localdate().isoformat(),
            "task_title": "回答下午注意力问题",
            "task_description": "根据已确认证据制作一条 TikTok 内容，并保存外部链接。",
        }
        deny = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref=self.account.account_code,
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            effect=PermissionGrant.Effect.DENY,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(days=1),
            granted_by_principal=self.owner,
        )
        blocked_data = dict(plan_data, command_id=str(uuid.uuid4()))
        blocked = self.client.post(plan_url, blocked_data, follow=True)
        self.assertContains(blocked, "不能建立平台任务安排")
        self.assertFalse(ChannelPlan.objects.filter(initiative=initiative).exists())
        revoke_permission_grant(
            actor=self.owner,
            grant_id=deny.pk,
            reason="Negative authorization test complete.",
        )
        self.assertEqual(self.client.post(plan_url, plan_data).status_code, 302)
        self.client.post(plan_url, plan_data)
        self.assertEqual(ChannelPlan.objects.filter(initiative=initiative).count(), 1)
        plan = ChannelPlan.objects.get(initiative=initiative)
        self._transition("plan-transition", plan.pk, "READY", 0, "账号和交付要求已确认。")
        plan.refresh_from_db()

        compile_url = reverse("dailyops:plan-compile", args=[plan.pk])
        compile_data = {"command_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4())}
        compiled = self.client.post(compile_url, compile_data)
        self.assertEqual(compiled.status_code, 302)
        self.assertEqual(Task.objects.filter(product=self.product).count(), 1)
        self.assertEqual(TaskCompilationContext.objects.filter(channel_plan=plan).count(), 1)
        task = Task.objects.get(product=self.product)
        self.assertEqual(compiled.url, reverse("dashboard:task-detail", args=[task.pk]))

        task_detail = self.client.get(compiled.url)
        self.assertContains(task_detail, "Daily Operations 编译上下文（只读）")
        self.assertContains(task_detail, "TIKTOK")
        self.assertContains(task_detail, self.account.display_name)
        self.assertContains(task_detail, self.account.account_code)
        self.assertContains(task_detail, "daily-ui-staging")
        self.assertContains(task_detail, CapabilityState.MANUAL_PUBLISH)
        self.assertContains(task_detail, "回答下午注意力问题")

        self.client.post(compile_url, compile_data)
        self.assertEqual(Task.objects.filter(product=self.product).count(), 1)

    def test_analysis_without_evidence_is_refused(self):
        batch_key, _, _ = self._start()
        response = self.client.post(
            reverse("dailyops:analysis-propose", args=[self.product.pk, batch_key]),
            {"command_id": str(uuid.uuid4())},
            follow=True,
        )
        self.assertContains(response, "至少先保存一条")
        self.assertFalse(SignalAssessment.objects.exists())

    def test_automatic_collection_button_defaults_to_fail_closed_and_keeps_fallback(self):
        batch_key, _, _ = self._start()
        command_id = uuid.uuid4()
        response = self.client.post(
            reverse("dailyops:automatic-collect", args=[self.product.pk, batch_key]),
            {"command_id": str(command_id)},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "没有可用的 API 凭据或已配对浏览器")
        self.assertContains(response, "CSV")
        self.assertEqual(CollectionRun.objects.filter(batch_key=batch_key).count(), 14)
        automatic = CollectionRun.objects.filter(
            batch_key=batch_key,
            query_spec__automatic_command_id=str(command_id),
        )
        self.assertEqual(automatic.count(), 7)
        self.assertTrue(
            all(run.result_summary["fallback"] == "CSV_OR_MANUAL" for run in automatic)
        )

    def test_progressive_collection_returns_one_honest_platform_result(self):
        batch_key, _, _ = self._start()
        detail = self.client.get(
            reverse("dailyops:batch-detail", args=[self.product.pk, batch_key])
        )
        self.assertContains(detail, "static/js/dailyops_collection.js")
        self.assertContains(detail, "data-collect-url=", count=7)
        command_id = uuid.uuid4()
        response = self.client.post(
            reverse(
                "dailyops:platform-collect",
                args=[self.product.pk, batch_key, "PINTEREST"],
            ),
            {"command_id": str(command_id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["platform"], "PINTEREST")
        self.assertTrue(response.json()["fallback"])
        self.assertEqual(
            CollectionRun.objects.filter(
                batch_key=batch_key,
                query_spec__automatic_command_id=str(command_id),
            ).count(),
            1,
        )

    def test_one_manual_entry_detects_platform_and_can_be_removed_without_deleting_history(self):
        batch_key, _, _ = self._start()
        response = self.client.post(
            reverse("dailyops:evidence-manual-unified", args=[self.product.pk, batch_key]),
            {
                "command_id": str(uuid.uuid4()),
                "collected_at": timezone.now().isoformat(),
                "reference": "https://www.tiktok.com/@puko/video/999",
                "platform": "",
                "title": "",
                "content_text": "",
            },
            follow=True,
        )
        self.assertContains(response, "已识别为 TikTok")
        evidence = ExternalEvidenceItem.objects.get(collection_run__batch_key=batch_key)
        self.assertEqual(evidence.platform_code, "TIKTOK")
        self.assertEqual(evidence.title, "https://www.tiktok.com/@puko/video/999")

        removed = self.client.post(
            reverse(
                "dailyops:evidence-invalidate",
                args=[self.product.pk, batch_key, evidence.pk],
            ),
            {"command_id": str(uuid.uuid4())},
            follow=True,
        )
        self.assertContains(removed, "已从本次分析中移除")
        self.assertTrue(ExternalEvidenceItem.objects.filter(pk=evidence.pk).exists())
        self.assertTrue(EvidenceInvalidationEvent.objects.filter(evidence_item=evidence).exists())
        detail = self.client.get(reverse("dailyops:batch-detail", args=[self.product.pk, batch_key]))
        self.assertNotContains(detail, "https://www.tiktok.com/@puko/video/999")

    def test_manual_entry_does_not_guess_unknown_or_conflicting_platform(self):
        batch_key, _, _ = self._start()
        url = reverse("dailyops:evidence-manual-unified", args=[self.product.pk, batch_key])
        unknown = self.client.post(
            url,
            {
                "command_id": str(uuid.uuid4()),
                "collected_at": timezone.now().isoformat(),
                "reference": "https://example.com/post/1",
                "platform": "",
            },
            follow=True,
        )
        self.assertContains(unknown, "无法从这条内容识别平台")
        conflict = self.client.post(
            url,
            {
                "command_id": str(uuid.uuid4()),
                "collected_at": timezone.now().isoformat(),
                "reference": "https://www.pinterest.com/pin/123/",
                "platform": "TIKTOK",
            },
            follow=True,
        )
        self.assertContains(conflict, "与你选择的 TikTok 不一致")
        self.assertFalse(ExternalEvidenceItem.objects.filter(collection_run__batch_key=batch_key).exists())

    def test_owner_one_click_starts_project_with_separate_events_and_is_replay_safe(self):
        batch_key, proposal = self._pending_proposal()
        detail = self.client.get(
            reverse("dailyops:batch-detail", args=[self.product.pk, batch_key])
        )
        self.assertContains(detail, "采用建议并建立执行项目")
        self.assertContains(
            detail,
            ".daily-stage.is-complete:not(.is-manually-open)",
        )
        self.assertContains(
            detail,
            'class="daily-stage is-complete" data-step="2"',
        )
        self.assertContains(detail, "stage.classList.add('is-manually-open')")

        command_id = uuid.uuid4()
        url = reverse(
            "dailyops:analysis-accept-and-start",
            args=[self.product.pk, batch_key, proposal.pk],
        )
        response = self.client.post(url, {"command_id": str(command_id)}, follow=True)
        self.assertContains(response, "已采用建议并建立执行项目")

        opportunity = ProductOpportunity.objects.get(opportunity_key=f"daily-{batch_key.hex}")
        initiative = Initiative.objects.get(opportunity=opportunity)
        self.assertEqual(opportunity.current_state, ProductOpportunity.State.APPROVED)
        self.assertEqual(initiative.current_state, Initiative.State.ACTIVE)
        self.assertEqual(
            list(
                opportunity.state_events.order_by("sequence").values_list(
                    "from_state", "to_state"
                )
            ),
            [
                (ProductOpportunity.State.PROPOSED, ProductOpportunity.State.TRIAGED),
                (ProductOpportunity.State.TRIAGED, ProductOpportunity.State.APPROVED),
            ],
        )
        self.assertEqual(
            list(
                initiative.state_events.order_by("sequence").values_list(
                    "from_state", "to_state"
                )
            ),
            [
                (Initiative.State.PROPOSED, Initiative.State.APPROVED),
                (Initiative.State.APPROVED, Initiative.State.ACTIVE),
            ],
        )
        all_events = [*opportunity.state_events.all(), *initiative.state_events.all()]
        self.assertTrue(
            all(
                event.principal_id == self.owner.pk and event.permission_grant_id
                for event in all_events
            )
        )
        self.assertEqual(
            initiative.creation_command_id,
            uuid.uuid5(command_id, "owner-initiative-created"),
        )

        replay = self.client.post(url, {"command_id": str(command_id)}, follow=True)
        self.assertContains(replay, "已采用建议并建立执行项目")
        self.assertEqual(ProductOpportunity.objects.filter(product=self.product).count(), 1)
        self.assertEqual(Initiative.objects.filter(opportunity=opportunity).count(), 1)
        self.assertEqual(OpportunityStateEvent.objects.filter(opportunity=opportunity).count(), 2)
        self.assertEqual(InitiativeStateEvent.objects.filter(initiative=initiative).count(), 2)

    def test_batch_stepper_uses_the_server_current_step_as_its_only_truth(self):
        batch_key, proposal = self._pending_proposal()
        self.client.post(
            reverse(
                "dailyops:analysis-accept",
                args=[self.product.pk, batch_key, proposal.pk],
            ),
            {"command_id": str(uuid.uuid4())},
        )
        opportunity = ProductOpportunity.objects.get(
            opportunity_key=f"daily-{batch_key.hex}"
        )
        self._transition(
            "opportunity-transition",
            opportunity.pk,
            ProductOpportunity.State.TRIAGED,
            0,
            "人工初审。",
        )
        opportunity.refresh_from_db()
        self._transition(
            "opportunity-transition",
            opportunity.pk,
            ProductOpportunity.State.APPROVED,
            1,
            "人工确认值得继续，但尚未建立执行项目。",
        )

        detail = self.client.get(
            reverse("dailyops:batch-detail", args=[self.product.pk, batch_key])
        )

        self.assertEqual(detail.context["current_step"], 5)
        self.assertContains(detail, 'data-current-step="5"')
        self.assertContains(
            detail,
            "const current = Number.parseInt(flow.dataset.currentStep, 10);",
        )
        self.assertNotContains(
            detail,
            "if (flow.dataset.hasOpportunity === '1') current = 4;",
        )
        self.assertNotContains(detail, "data-has-opportunity")

    def test_owner_project_shortcut_rolls_back_all_facts_when_later_step_fails(self):
        batch_key, proposal = self._pending_proposal()
        url = reverse(
            "dailyops:analysis-accept-and-start",
            args=[self.product.pk, batch_key, proposal.pk],
        )
        with patch(
            "dailyops.services.create_initiative_from_opportunity",
            side_effect=ValidationError("forced later-step failure"),
        ):
            response = self.client.post(
                url,
                {"command_id": str(uuid.uuid4())},
                follow=True,
            )
        self.assertContains(response, "不能采用建议并建立执行项目")
        self.assertFalse(ProductOpportunity.objects.filter(product=self.product).exists())
        self.assertFalse(Initiative.objects.filter(product=self.product).exists())
        self.assertFalse(OpportunityStateEvent.objects.exists())
        self.assertFalse(
            SignalAssessment.objects.filter(supersedes=proposal).exists()
        )

    def test_owner_one_click_confirms_plan_compiles_task_and_is_replay_safe(self):
        batch_key, initiative = self._approved_initiative()
        self.client.post(
            reverse("dailyops:plan-create", args=[initiative.pk]),
            self._plan_data(),
        )
        plan = ChannelPlan.objects.get(initiative=initiative)
        detail = self.client.get(
            reverse("dailyops:batch-detail", args=[self.product.pk, batch_key])
        )
        self.assertContains(detail, "确认平台安排并生成执行任务")

        command_id = uuid.uuid4()
        task_id = uuid.uuid4()
        url = reverse("dailyops:plan-confirm-and-compile", args=[plan.pk])
        response = self.client.post(
            url,
            {"command_id": str(command_id), "task_id": str(task_id)},
        )
        plan.refresh_from_db()
        self.assertEqual(plan.current_state, ChannelPlan.State.ACTIVE)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(
                plan.state_events.order_by("sequence").values_list(
                    "from_state", "to_state"
                )
            ),
            [
                (ChannelPlan.State.DRAFT, ChannelPlan.State.READY),
                (ChannelPlan.State.READY, ChannelPlan.State.ACTIVE),
            ],
        )
        self.assertTrue(
            all(
                event.principal_id == self.owner.pk and event.permission_grant_id
                for event in plan.state_events.all()
            )
        )
        context = TaskCompilationContext.objects.get(channel_plan=plan)
        self.assertEqual(context.task_id, task_id)
        self.assertEqual(
            context.compilation_command_id,
            uuid.uuid5(command_id, "owner-channel-plan-compiled"),
        )
        self.assertEqual(response.url, reverse("dashboard:task-detail", args=[task_id]))

        replay = self.client.post(
            url,
            {"command_id": str(command_id), "task_id": str(task_id)},
        )
        self.assertEqual(replay.status_code, 302)
        self.assertEqual(Task.objects.filter(pk=task_id).count(), 1)
        self.assertEqual(TaskCompilationContext.objects.filter(channel_plan=plan).count(), 1)
        self.assertEqual(ChannelPlanStateEvent.objects.filter(channel_plan=plan).count(), 2)

    def test_plan_shortcut_permission_failure_rolls_back_ready_and_active_events(self):
        _, initiative = self._approved_initiative()
        self.client.post(
            reverse("dailyops:plan-create", args=[initiative.pk]),
            self._plan_data(),
        )
        plan = ChannelPlan.objects.get(initiative=initiative)
        PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.CREATE_TASK,
            effect=PermissionGrant.Effect.DENY,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(days=1),
            granted_by_principal=self.owner,
        )
        response = self.client.post(
            reverse("dailyops:plan-confirm-and-compile", args=[plan.pk]),
            {"command_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4())},
        )
        self.assertEqual(response.status_code, 403)
        plan.refresh_from_db()
        self.assertEqual(plan.current_state, ChannelPlan.State.DRAFT)
        self.assertEqual(plan.state_version, 0)
        self.assertFalse(ChannelPlanStateEvent.objects.filter(channel_plan=plan).exists())
        self.assertFalse(TaskCompilationContext.objects.filter(channel_plan=plan).exists())
        self.assertFalse(Task.objects.filter(product=self.product).exists())

    def test_compiled_task_cross_role_web_flow_reaches_operator_today_and_admin_review(self):
        operator = Principal.objects.create_user(
            username="daily-flow-operator",
            password="safe-local-password-789",
            role=Principal.Role.OPERATOR,
            display_name="Daily Flow Operator",
        )
        admin = Principal.objects.create_user(
            username="daily-flow-admin",
            password="safe-local-password-987",
            role=Principal.Role.OPERATIONS_ADMIN,
            display_name="Daily Flow Admin",
        )
        now = timezone.now()

        def allow(principal, action):
            return PermissionGrant.objects.create(
                principal=principal,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                product=self.product,
                action=action,
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(days=1),
                granted_by_principal=self.owner,
            )

        allow(self.owner, PermissionGrant.Action.ASSIGN_TASK)
        allow(operator, PermissionGrant.Action.EDIT)
        allow(admin, PermissionGrant.Action.REVIEW)
        allow(admin, PermissionGrant.Action.EDIT)

        _, initiative = self._approved_initiative()
        self.client.post(
            reverse("dailyops:plan-create", args=[initiative.pk]),
            self._plan_data(),
        )
        plan = ChannelPlan.objects.get(initiative=initiative)
        task_id = uuid.uuid4()
        compiled = self.client.post(
            reverse("dailyops:plan-confirm-and-compile", args=[plan.pk]),
            {"command_id": str(uuid.uuid4()), "task_id": str(task_id)},
        )
        self.assertEqual(compiled.status_code, 302)
        task = Task.objects.get(pk=task_id)

        def command_data(**extra):
            task.refresh_from_db()
            return {
                "command_id": str(uuid.uuid4()),
                "expected_state_version": task.state_version,
                **extra,
            }

        self.client.force_login(self.owner)
        dor = self.client.post(
            reverse("dashboard:task-action", args=[task.pk, "dor"]),
            command_data(criterion__source_ready=TaskCheckRun.Result.PASS),
        )
        self.assertEqual(dor.status_code, 302)
        assigned = self.client.post(
            reverse("dashboard:task-action", args=[task.pk, "assign"]),
            command_data(assignee=str(operator.pk)),
        )
        self.assertEqual(assigned.status_code, 302)

        self.client.force_login(operator)
        operator_today = self.client.get(reverse("dashboard:home"))
        self.assertEqual(operator_today.status_code, 200)
        self.assertContains(operator_today, task.title)
        self.assertIn(task.pk, {item.pk for item in operator_today.context["tasks"]})
        started = self.client.post(
            reverse("dashboard:task-action", args=[task.pk, "start"]),
            command_data(),
        )
        self.assertEqual(started.status_code, 302)
        submitted = self.client.post(
            reverse("dashboard:task-action", args=[task.pk, "deliver"]),
            command_data(
                external_url="https://docs.example.test/daily-flow/v1",
                submission_note="Ready for independent admin review.",
                criterion__external_link=TaskCheckRun.Result.PASS,
            ),
        )
        self.assertEqual(submitted.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.current_state, Task.State.UNDER_REVIEW)

        self.client.force_login(admin)
        admin_today = self.client.get(reverse("dashboard:home"))
        self.assertEqual(admin_today.status_code, 200)
        self.assertEqual(admin_today.context["pending_review_count"], 1)
        self.assertContains(admin_today, "等我审核")
        review_queue = self.client.get(reverse("dashboard:review-queue"))
        self.assertEqual(review_queue.status_code, 200)
        self.assertContains(review_queue, task.title)

    def test_product_without_realtime_grant_is_hidden(self):
        batch_key, _, _ = self._start()
        self.client.force_login(self.outsider)
        response = self.client.get(
            reverse("dailyops:batch-detail", args=[self.product.pk, batch_key])
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            reverse("dailyops:evidence-manual", args=[self.product.pk, batch_key, "TIKTOK"]),
            {
                "command_id": str(uuid.uuid4()),
                "collected_at": timezone.now().isoformat(),
                "external_url": "https://example.com/no-access",
                "title": "No access",
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_post_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        response = csrf_client.post(
            reverse("dailyops:batch-start"),
            {
                "command_id": str(uuid.uuid4()),
                "product": str(self.product.pk),
                "query": "focus",
                "window_start": "2026-08-01T00:00",
                "window_end": "2026-08-02T00:00",
            },
        )
        self.assertEqual(response.status_code, 403)
