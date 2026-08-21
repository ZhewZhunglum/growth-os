from __future__ import annotations

import uuid
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from accounts.services import revoke_permission_grant
from dailyops.services import PLATFORMS, ensure_default_sources
from intelligence.models import (
    ChannelPlan,
    CollectionRun,
    ExternalEvidenceItem,
    Initiative,
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
from workflow.models import Task, TaskContractPolicyLink, TaskContractVersion


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
        environment = RuntimeEnvironment.objects.create(
            environment_code="daily-ui-staging",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="daily-ui",
            database_namespace="daily-ui",
            object_storage_namespace="link-only",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        binding = AccountEnvironmentBinding.objects.create(
            channel_account=self.account,
            runtime_environment=environment,
            binding_version=1,
            identity_reference="daily-ui-browser-profile",
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        CapabilityState.objects.create(
            account_environment_binding=binding,
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

    def test_source_setup_is_offline_idempotent_and_requires_global_management(self):
        response = self.client.post(reverse("dailyops:source-setup"))
        self.assertRedirects(response, reverse("dailyops:home"))
        self.assertEqual(SourceRegistry.objects.filter(source_key__startswith="daily-").count(), 28)
        self.assertFalse(CollectionRun.objects.exists())

        self.client.force_login(self.outsider)
        denied = self.client.post(reverse("dailyops:source-setup"))
        self.assertEqual(denied.status_code, 403)

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
            "channel_account": str(self.account.pk),
            "plan_date": timezone.localdate().isoformat(),
            "task_title": "回答下午注意力问题",
            "task_description": "根据已确认证据制作一条 TikTok 内容，并保存外部链接。",
            "environment_code": "daily-ui-staging",
            "capability_code": CapabilityState.MANUAL_PUBLISH,
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
        self.assertContains(blocked, "不能创建 ChannelPlan")
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
