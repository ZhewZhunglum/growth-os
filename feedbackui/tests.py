from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from governance.models import PolicyActivation
from insights.models import (
    AvailabilityState,
    ChannelPerformanceObservation,
    DataDomain,
    GEOMetricObservation,
    GEOProbePanel,
    GEOProbePanelItem,
    LearningEvidenceLink,
    LearningVersion,
    MetricDefinition,
)
from intelligence.models import Initiative
from products.models import Product
from releasegate.models import ChannelAccount

from .forms import PerformanceCsvForm
from .services import (
    add_geo_panel_item,
    create_geo_panel_version,
    propose_learning,
    record_geo_result,
    record_performance_rows,
)


class FeedbackUiTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="feedback-owner",
            password="safe-test-password-123",
            role=Principal.Role.OWNER,
        )
        self.operator = Principal.objects.create_user(
            username="feedback-operator",
            password="safe-test-password-123",
            role=Principal.Role.OPERATOR,
        )
        self.product = Product.objects.create(
            product_code="PUKO-FEEDBACK",
            name="PUKO Feedback",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.other_product = Product.objects.create(
            product_code="PUKO-OTHER",
            name="Other Product",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.account = ChannelAccount.objects.create(
            platform_code="tiktok",
            account_code="puko-feedback-tiktok",
            external_account_ref="tt-feedback",
            display_name="PUKO Feedback TikTok",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.account_collect_grant = self.grant(
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref=self.account.account_code,
        )
        self.product_collect_grant = self.grant(
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.product_edit_grant = self.grant(
            action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.panel = GEOProbePanel.objects.create(
            panel_key="puko-feedback-panel",
            version_number=1,
            product=self.product,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
        )
        self.panel_item = GEOProbePanelItem.objects.create(
            panel=self.panel,
            item_number=1,
            question="What helps with focus?",
            intent="product discovery",
        )

    def grant(self, *, action, scope_kind, product=None, account_ref=""):
        return PermissionGrant.objects.create(
            principal=self.operator,
            scope_kind=scope_kind,
            product=product,
            account_ref=account_ref,
            action=action,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.LOW,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(days=1),
            granted_by_principal=self.owner,
        )

    def performance_row(self, *, state=AvailabilityState.PRESENT, value=Decimal("0"), key="views"):
        return {
            "metric_key": key,
            "metric_name": key.title(),
            "availability_state": state,
            "numeric_value": value,
            "unit": "count",
            "observed_at": timezone.now(),
            "source_reference": "manual test",
        }

    def create_geo(self, *, state=AvailabilityState.PRESENT, brand_mentioned=False):
        return record_geo_result(
            actor=self.operator,
            panel_item=self.panel_item,
            provider="DeepSeek",
            model_reference="deepseek-v4-flash",
            availability_state=state,
            response_text="A bounded manual answer." if state == AvailabilityState.PRESENT else "",
            brand_mentioned=brand_mentioned,
            rank_position=2 if state == AvailabilityState.PRESENT and brand_mentioned else None,
            citation_urls=["https://example.com/source"] if state == AvailabilityState.PRESENT else [],
            operation_key=str(uuid.uuid4()),
        )

    def test_manual_performance_preserves_real_zero_and_exact_grant(self):
        observations = record_performance_rows(
            actor=self.operator,
            channel_account=self.account,
            rows=[self.performance_row()],
            source_kind="MANUAL",
            operation_key=str(uuid.uuid4()),
        )
        observation = observations[0]
        self.assertEqual(observation.availability_state, AvailabilityState.PRESENT)
        self.assertEqual(observation.numeric_value, Decimal("0"))
        self.assertEqual(
            observation.collection_run.parameters["permission_grant_id"],
            str(self.account_collect_grant.pk),
        )
        observation.numeric_value = Decimal("1")
        with self.assertRaises(ValidationError):
            observation.save()

    def test_missing_is_not_converted_to_zero(self):
        observation = record_performance_rows(
            actor=self.operator,
            channel_account=self.account,
            rows=[self.performance_row(state=AvailabilityState.MISSING, value=None)],
            source_kind="MANUAL",
            operation_key=str(uuid.uuid4()),
        )[0]
        self.assertEqual(observation.availability_state, AvailabilityState.MISSING)
        self.assertIsNone(observation.numeric_value)

    def test_csv_paste_is_bounded_validated_and_idempotent(self):
        operation_key = str(uuid.uuid4())
        form = PerformanceCsvForm(
            {
                "operation_key": operation_key,
                "channel_account": self.account.pk,
                "csv_text": (
                    "metric_key,metric_name,availability_state,numeric_value,unit\n"
                    "views,Views,PRESENT,0,count\n"
                    "clicks,Clicks,MISSING,,count\n"
                ),
            },
            actor=self.operator,
        )
        self.assertTrue(form.is_valid(), form.errors)
        first = record_performance_rows(
            actor=self.operator,
            channel_account=form.cleaned_data["channel_account"],
            rows=form.csv_rows,
            source_kind="CSV",
            operation_key=operation_key,
        )
        replay = record_performance_rows(
            actor=self.operator,
            channel_account=form.cleaned_data["channel_account"],
            rows=form.csv_rows,
            source_kind="CSV",
            operation_key=operation_key,
        )
        self.assertEqual(len(first), 2)
        self.assertEqual({item.pk for item in first}, {item.pk for item in replay})
        self.assertEqual(ChannelPerformanceObservation.objects.count(), 2)
        self.assertEqual(first[0].numeric_value, Decimal("0"))
        self.assertIsNone(first[1].numeric_value)

    def test_domain_collision_and_unauthorized_write_fail_closed(self):
        MetricDefinition.objects.create(
            metric_key="views",
            version_number=1,
            name="Wrong domain views",
            data_domain=DataDomain.GEO,
            value_kind=MetricDefinition.ValueKind.COUNT,
            unit="count",
            created_by_principal=self.owner,
        )
        before = ChannelPerformanceObservation.objects.count()
        with self.assertRaises(ValidationError):
            record_performance_rows(
                actor=self.operator,
                channel_account=self.account,
                rows=[self.performance_row()],
                source_kind="MANUAL",
                operation_key=str(uuid.uuid4()),
            )
        self.assertEqual(ChannelPerformanceObservation.objects.count(), before)

        outsider = Principal.objects.create_user(
            username="feedback-outsider",
            password="safe-test-password-123",
            role=Principal.Role.OPERATOR,
        )
        with self.assertRaises(PermissionDenied):
            record_performance_rows(
                actor=outsider,
                channel_account=self.account,
                rows=[self.performance_row(key="clicks")],
                source_kind="MANUAL",
                operation_key=str(uuid.uuid4()),
            )

    def test_geo_result_creates_separate_geo_metric_and_preserves_zero(self):
        result = self.create_geo(brand_mentioned=False)
        metric = GEOMetricObservation.objects.get(probe_result=result)
        self.assertEqual(metric.data_domain, DataDomain.GEO)
        self.assertEqual(metric.availability_state, AvailabilityState.PRESENT)
        self.assertEqual(metric.numeric_value, Decimal("0"))
        self.assertEqual(
            metric.collection_run.parameters["permission_grant_id"],
            str(self.product_collect_grant.pk),
        )
        self.assertEqual(result.citations.get().cited_domain, "example.com")

    def test_geo_missing_has_no_numeric_value(self):
        result = self.create_geo(state=AvailabilityState.MISSING)
        metric = GEOMetricObservation.objects.get(probe_result=result)
        self.assertEqual(metric.availability_state, AvailabilityState.MISSING)
        self.assertIsNone(metric.numeric_value)
        self.assertFalse(result.brand_mentioned)

    def test_learning_is_only_proposed_and_links_exact_geo_evidence(self):
        result = self.create_geo(brand_mentioned=True)
        evidence = GEOMetricObservation.objects.get(probe_result=result)
        learning = propose_learning(
            actor=self.operator,
            product=self.product,
            learning_key="geo-mention-test",
            title="PUKO was mentioned in this bounded GEO probe",
            conclusion="One manually recorded answer mentioned PUKO.",
            recommended_action="Propose another bounded test; do not change policy automatically.",
            confidence=Decimal("0.5000"),
            evidence_ref=f"geo:{evidence.pk}",
            evidence_note="Exact GEO metric observation.",
        )
        self.assertEqual(learning.status, LearningVersion.Status.PROPOSED)
        link = LearningEvidenceLink.objects.get(learning_version=learning)
        self.assertEqual(link.geo_metric_id, evidence.pk)
        self.assertEqual(Initiative.objects.count(), 0)
        self.assertEqual(PolicyActivation.objects.count(), 0)

        self.grant(
            action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.other_product,
        )
        with self.assertRaises(ValidationError):
            propose_learning(
                actor=self.operator,
                product=self.other_product,
                learning_key="wrong-product",
                title="Wrong product",
                conclusion="Must fail before creating anything.",
                recommended_action="None",
                confidence=Decimal("0.5000"),
                evidence_ref=f"geo:{evidence.pk}",
                evidence_note="",
            )

    def test_home_and_manual_post_use_server_side_permissions(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("feedback:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "真实的 0")
        self.assertContains(response, "AI 搜索曝光与结果")
        self.assertContains(response, "手动记录一次 AI 搜索结果")
        self.assertContains(response, "高级录入（通常不用打开）")
        response = self.client.post(
            reverse("feedback:performance-manual"),
            {
                "operation_key": str(uuid.uuid4()),
                "channel_account": self.account.pk,
                "metric_key": "shares",
                "metric_name": "Shares",
                "availability_state": AvailabilityState.PRESENT,
                "numeric_value": "0",
                "unit": "count",
                "source_reference": "manual UI test",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ChannelPerformanceObservation.objects.filter(metric_definition__metric_key="shares").exists())

    def test_feedback_page_switches_all_visible_ui_and_form_copy_to_english(self):
        self.client.force_login(self.operator)
        self.client.post(
            reverse("set_language"),
            {"language": "en", "next": reverse("feedback:home")},
        )

        response = self.client.get(reverse("feedback:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Search Visibility & Results")
        self.assertContains(response, "Will AI answers mention PUKO?")
        self.assertContains(response, "Record one AI search result")
        self.assertContains(response, "Where did you ask?")
        self.assertContains(response, "Did you receive an answer?")
        self.assertContains(response, "Advanced entry (usually leave closed)")
        self.assertContains(response, "Other result tools: platform performance and proposals")
        self.assertNotContains(response, "手动记录一次 AI 搜索结果")
        self.assertNotContains(response, "这次拿到回答了吗")

    def test_geo_form_validation_and_success_message_follow_selected_language(self):
        self.client.force_login(self.operator)
        self.client.post(
            reverse("set_language"),
            {"language": "en", "next": reverse("feedback:home")},
        )
        operation_key = str(uuid.uuid4())

        invalid = self.client.post(
            reverse("feedback:geo-result"),
            {
                "operation_key": operation_key,
                "panel_item": self.panel_item.pk,
                "provider": "DeepSeek",
                "model_reference": "",
                "availability_state": AvailabilityState.PRESENT,
                "response_text": "",
                "citation_urls": "",
            },
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertContains(
            invalid,
            "Paste the AI answer when a result is available.",
            status_code=400,
        )

        saved = self.client.post(
            reverse("feedback:geo-result"),
            {
                "operation_key": operation_key,
                "panel_item": self.panel_item.pk,
                "provider": "DeepSeek",
                "model_reference": "",
                "availability_state": AvailabilityState.PRESENT,
                "response_text": "PUKO is one option.",
                "brand_mentioned": "on",
                "citation_urls": "https://example.com/result",
            },
            follow=True,
        )

        self.assertEqual(saved.status_code, 200)
        self.assertContains(saved, "The AI search result was saved.")
        self.assertContains(saved, "PUKO mentioned")

    def test_owner_can_append_replay_safe_geo_panel_versions_and_items(self):
        PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            effect=PermissionGrant.Effect.ALLOW,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(days=1),
            granted_by_principal=self.owner,
        )
        panel, created = create_geo_panel_version(
            actor=self.owner,
            product=self.product,
            panel_key="puko-feedback-panel",
            version_number=2,
            market_code="us",
            language_code="EN",
        )
        replay, replay_created = create_geo_panel_version(
            actor=self.owner,
            product=self.product,
            panel_key="puko-feedback-panel",
            version_number=2,
            market_code="US",
            language_code="en",
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, panel.pk)
        self.assertEqual(GEOProbePanel.objects.filter(panel_key="puko-feedback-panel").count(), 2)

        item, item_created = add_geo_panel_item(
            actor=self.owner,
            panel=panel,
            item_number=1,
            question="Which products support afternoon focus?",
            intent="product discovery",
        )
        item_replay, item_replay_created = add_geo_panel_item(
            actor=self.owner,
            panel=panel,
            item_number=1,
            question="Which products support afternoon focus?",
            intent="product discovery",
        )
        self.assertTrue(item_created)
        self.assertFalse(item_replay_created)
        self.assertEqual(item_replay.pk, item.pk)

        with self.assertRaises(ValidationError):
            add_geo_panel_item(
                actor=self.owner,
                panel=panel,
                item_number=1,
                question="A different question must not overwrite item 1.",
                intent="product discovery",
            )
        item.question = "Mutated question"
        with self.assertRaises(ValidationError):
            item.save()

    def test_operator_cannot_configure_geo_panel_even_with_product_edit(self):
        with self.assertRaises(PermissionDenied):
            create_geo_panel_version(
                actor=self.operator,
                product=self.product,
                panel_key="operator-panel",
                version_number=1,
                market_code="US",
                language_code="en",
            )

        self.client.force_login(self.operator)
        page = self.client.get(reverse("feedback:home"))
        self.assertNotContains(page, "设置一组要问 AI 的问题")
        forged = self.client.post(
            reverse("feedback:geo-panel-version"),
            {
                "panel_key": "operator-panel",
                "version_number": 1,
                "product": self.product.pk,
                "market_code": "US",
                "language_code": "en",
            },
        )
        self.assertEqual(forged.status_code, 400)
        self.assertFalse(GEOProbePanel.objects.filter(panel_key="operator-panel").exists())

    def test_owner_can_configure_geo_panel_without_django_admin(self):
        PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            effect=PermissionGrant.Effect.ALLOW,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(days=1),
            granted_by_principal=self.owner,
        )
        self.assertFalse(self.owner.is_superuser)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("feedback:home"))
        self.assertContains(page, "设置一组要问 AI 的问题")
        created = self.client.post(
            reverse("feedback:geo-panel-version"),
            {
                "panel_key": "ui-created-panel",
                "version_number": 1,
                "product": self.product.pk,
                "market_code": "US",
                "language_code": "en",
            },
        )
        self.assertRedirects(created, reverse("feedback:home"))
        panel = GEOProbePanel.objects.get(panel_key="ui-created-panel")
        added = self.client.post(
            reverse("feedback:geo-panel-item"),
            {
                "panel": panel.pk,
                "item_number": 1,
                "question": "What helps people stay focused?",
                "intent": "discovery",
            },
        )
        self.assertRedirects(added, reverse("feedback:home"))
        self.assertTrue(panel.items.filter(item_number=1).exists())
