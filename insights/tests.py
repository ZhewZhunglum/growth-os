from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import Principal
from products.models import Product
from releasegate.models import ChannelAccount

from .models import (
    AvailabilityState,
    ChannelPerformanceObservation,
    DataDomain,
    LearningEvidenceLink,
    LearningVersion,
    MetricCollectionRun,
    MetricCollectionRunMetric,
    MetricDefinition,
)
from .services import decide_learning


class InsightFactTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="insights-owner",
            password="safe-test-password-123",
            role=Principal.Role.OWNER,
        )
        self.product = Product.objects.create(
            product_code="PUKO-INSIGHTS",
            name="PUKO Insights",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.account = ChannelAccount.objects.create(
            platform_code="tiktok",
            account_code="puko-tiktok-insights",
            external_account_ref="tt-insights",
            display_name="PUKO TikTok",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )

    def metric(self, domain=DataDomain.CONTENT_PERFORMANCE):
        return MetricDefinition.objects.create(
            metric_key=f"views-{domain.lower()}",
            version_number=1,
            name="Views",
            data_domain=domain,
            value_kind=MetricDefinition.ValueKind.COUNT,
            unit="count",
            created_by_principal=self.owner,
        )

    def collection_run(self, domain=DataDomain.CONTENT_PERFORMANCE):
        now = timezone.now()
        return MetricCollectionRun.objects.create(
            run_key=f"run-{domain.lower()}",
            data_domain=domain,
            source_kind=MetricCollectionRun.SourceKind.API,
            window_start=now - timedelta(days=1),
            window_end=now,
            status=MetricCollectionRun.Status.COMPLETED,
            started_at=now,
            completed_at=now,
            created_by_principal=self.owner,
        )

    def make_observation(self, *, value=Decimal("0"), availability=AvailabilityState.PRESENT):
        metric = self.metric()
        run = self.collection_run()
        MetricCollectionRunMetric.objects.create(
            collection_run=run,
            metric_definition=metric,
            data_domain=DataDomain.CONTENT_PERFORMANCE,
        )
        return ChannelPerformanceObservation.objects.create(
            channel_account=self.account,
            metric_definition=metric,
            collection_run=run,
            data_domain=DataDomain.CONTENT_PERFORMANCE,
            availability_state=availability,
            numeric_value=value,
            unit="count",
            observed_at=timezone.now(),
            recorded_by_principal=self.owner,
        )

    def test_zero_is_present_and_distinct_from_missing(self):
        zero = self.make_observation()
        self.assertEqual(zero.numeric_value, Decimal("0"))
        self.assertEqual(zero.availability_state, AvailabilityState.PRESENT)

        metric = zero.metric_definition
        run = zero.collection_run
        missing = ChannelPerformanceObservation.objects.create(
            channel_account=self.account,
            metric_definition=metric,
            collection_run=run,
            data_domain=DataDomain.CONTENT_PERFORMANCE,
            availability_state=AvailabilityState.MISSING,
            numeric_value=None,
            observed_at=timezone.now() + timedelta(minutes=1),
            recorded_by_principal=self.owner,
        )
        self.assertIsNone(missing.numeric_value)

    def test_observations_are_append_only_and_corrections_are_linked(self):
        original = self.make_observation(value=Decimal("12"))
        original.numeric_value = Decimal("13")
        with self.assertRaises(ValidationError):
            original.save()
        with self.assertRaises(ValidationError):
            ChannelPerformanceObservation.objects.filter(pk=original.pk).update(numeric_value=Decimal("13"))
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE insights_channelperformanceobservation SET numeric_value = %s WHERE id = %s",
                    [Decimal("13"), original.pk.hex],
                )

        invalidation = ChannelPerformanceObservation.objects.create(
            channel_account=self.account,
            metric_definition=original.metric_definition,
            collection_run=original.collection_run,
            data_domain=DataDomain.CONTENT_PERFORMANCE,
            availability_state=AvailabilityState.INVALIDATED,
            numeric_value=None,
            observed_at=timezone.now(),
            supersedes_observation=original,
            correction_reason="Provider corrected the reporting window.",
            recorded_by_principal=self.owner,
        )
        self.assertEqual(invalidation.supersedes_observation_id, original.pk)

    def test_domain_mismatch_is_fail_closed(self):
        content_metric = self.metric(DataDomain.CONTENT_PERFORMANCE)
        search_run = self.collection_run(DataDomain.SEARCH_VISIBILITY)
        with self.assertRaises(ValidationError):
            MetricCollectionRunMetric.objects.create(
                collection_run=search_run,
                metric_definition=content_metric,
                data_domain=DataDomain.SEARCH_VISIBILITY,
            )

        content_run = self.collection_run(DataDomain.CONTENT_PERFORMANCE)
        MetricCollectionRunMetric.objects.create(
            collection_run=content_run,
            metric_definition=content_metric,
            data_domain=DataDomain.CONTENT_PERFORMANCE,
        )
        with self.assertRaises(ValidationError):
            ChannelPerformanceObservation.objects.create(
                channel_account=self.account,
                metric_definition=content_metric,
                collection_run=content_run,
                data_domain=DataDomain.SEARCH_VISIBILITY,
                availability_state=AvailabilityState.PRESENT,
                numeric_value=Decimal("1"),
                observed_at=timezone.now(),
                recorded_by_principal=self.owner,
            )

    def test_learning_decision_appends_and_typed_evidence_rejects_mismatch(self):
        observation = self.make_observation(value=Decimal("10"))
        proposed = LearningVersion.objects.create(
            learning_key="learn-hook-short",
            version_number=1,
            product=self.product,
            title="Short hooks performed better",
            conclusion="Short hooks had more views in this bounded sample.",
            recommended_action="Propose another controlled test.",
            confidence=Decimal("0.7500"),
            created_by_principal=self.owner,
        )
        LearningEvidenceLink.objects.create(
            learning_version=proposed,
            source_kind=LearningEvidenceLink.SourceKind.CHANNEL_PERFORMANCE,
            channel_performance=observation,
        )
        with self.assertRaises(ValidationError):
            LearningEvidenceLink.objects.create(
                learning_version=proposed,
                source_kind=LearningEvidenceLink.SourceKind.GEO,
                channel_performance=observation,
            )

        approved = decide_learning(
            learning=proposed,
            decision=LearningVersion.Status.APPROVED,
            actor_principal=self.owner,
        )
        self.assertEqual(approved.version_number, 2)
        self.assertEqual(approved.supersedes_version_id, proposed.pk)
        proposed.refresh_from_db()
        self.assertEqual(proposed.status, LearningVersion.Status.PROPOSED)
