from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant
from insights.models import (
    AvailabilityState,
    ChannelPerformanceObservation,
    MetricCollectionRun,
    PublicationPerformanceObservation,
)
from releasegate import tests as releasegate_tests
from releasegate.models import ChannelAccount
from releasegate.services import orchestrate_v1_release_gate, record_manual_publication_proof

from .services import record_performance_rows


class _ReleaseFixture:
    _grant = releasegate_tests.ReleaseGateDomainTests._grant


class ExactPublicationPerformanceTests(TestCase):
    def setUp(self):
        fixture = _ReleaseFixture()
        releasegate_tests.ReleaseGateDomainTests.setUp(fixture)
        self.fixture = fixture
        self.ready = orchestrate_v1_release_gate(
            task=fixture.task,
            submission=fixture.submission,
            publisher_principal=fixture.publisher,
            channel_account=fixture.channel_account,
            runtime_environment=fixture.environment,
            command_id=uuid.uuid4(),
        ).publication
        self.collect_grant = self._collect_grant(fixture.channel_account)

    def _collect_grant(self, account):
        return PermissionGrant.objects.create(
            principal=self.fixture.publisher,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref=account.account_code,
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.LOW,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=2),
            granted_by_principal=self.fixture.owner,
        )

    def _row(self):
        return {
            "metric_key": "publication-views",
            "metric_name": "Publication views",
            "availability_state": AvailabilityState.PRESENT,
            "numeric_value": Decimal("0"),
            "unit": "count",
            "observed_at": timezone.now(),
            "source_reference": "manual exact publication test",
        }

    def _publish_manually(self):
        return record_manual_publication_proof(
            publication=self.ready,
            publisher_principal=self.fixture.publisher,
            command_id=uuid.uuid4(),
            external_publication_id="exact-performance-proof-1",
            external_url="https://www.tiktok.com/@puko/video/exact-performance-proof-1",
        )

    def test_manual_publication_records_exact_zero_and_replays_idempotently(self):
        self._publish_manually()
        row = self._row()
        operation_key = str(uuid.uuid4())

        first = record_performance_rows(
            actor=self.fixture.publisher,
            channel_account=self.fixture.channel_account,
            publication=self.ready,
            rows=[row],
            source_kind=MetricCollectionRun.SourceKind.MANUAL,
            operation_key=operation_key,
        )
        replay = record_performance_rows(
            actor=self.fixture.publisher,
            channel_account=self.fixture.channel_account,
            publication=self.ready,
            rows=[row],
            source_kind=MetricCollectionRun.SourceKind.MANUAL,
            operation_key=operation_key,
        )

        observation = first[0]
        self.assertEqual([item.pk for item in replay], [observation.pk])
        self.assertEqual(PublicationPerformanceObservation.objects.count(), 1)
        self.assertEqual(ChannelPerformanceObservation.objects.count(), 0)
        self.assertEqual(observation.publication_id, self.ready.pk)
        self.assertEqual(observation.numeric_value, Decimal("0"))
        self.assertEqual(observation.availability_state, AvailabilityState.PRESENT)
        self.assertEqual(
            observation.publication.current_gate.channel_account_id,
            self.fixture.channel_account.pk,
        )
        self.assertEqual(
            observation.collection_run.parameters["permission_grant_id"],
            str(self.collect_grant.pk),
        )

    def test_unpublished_publication_is_rejected_without_half_facts(self):
        before_runs = MetricCollectionRun.objects.count()
        before_observations = PublicationPerformanceObservation.objects.count()

        with self.assertRaises(ValidationError):
            record_performance_rows(
                actor=self.fixture.publisher,
                channel_account=self.fixture.channel_account,
                publication=self.ready,
                rows=[self._row()],
                source_kind=MetricCollectionRun.SourceKind.MANUAL,
                operation_key=str(uuid.uuid4()),
            )

        self.assertEqual(MetricCollectionRun.objects.count(), before_runs)
        self.assertEqual(
            PublicationPerformanceObservation.objects.count(), before_observations
        )

    def test_publication_account_mismatch_is_rejected_without_half_facts(self):
        self._publish_manually()
        other_account = ChannelAccount.objects.create(
            platform_code="TIKTOK",
            account_code="puko-other-account",
            external_account_ref="puko-other-account-public",
            display_name="PUKO Other",
            created_by_principal=self.fixture.owner,
            updated_by_principal=self.fixture.owner,
        )
        self._collect_grant(other_account)
        before_runs = MetricCollectionRun.objects.count()
        before_observations = PublicationPerformanceObservation.objects.count()

        with self.assertRaises(ValidationError):
            record_performance_rows(
                actor=self.fixture.publisher,
                channel_account=other_account,
                publication=self.ready,
                rows=[self._row()],
                source_kind=MetricCollectionRun.SourceKind.MANUAL,
                operation_key=str(uuid.uuid4()),
            )

        self.assertEqual(MetricCollectionRun.objects.count(), before_runs)
        self.assertEqual(
            PublicationPerformanceObservation.objects.count(), before_observations
        )

    def test_legacy_account_level_channel_performance_path_remains_compatible(self):
        observation = record_performance_rows(
            actor=self.fixture.publisher,
            channel_account=self.fixture.channel_account,
            rows=[self._row()],
            source_kind=MetricCollectionRun.SourceKind.MANUAL,
            operation_key=str(uuid.uuid4()),
        )[0]

        self.assertIsInstance(observation, ChannelPerformanceObservation)
        self.assertEqual(observation.channel_account_id, self.fixture.channel_account.pk)
        self.assertEqual(observation.numeric_value, Decimal("0"))
        self.assertEqual(PublicationPerformanceObservation.objects.count(), 0)
