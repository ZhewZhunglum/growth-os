from __future__ import annotations

import uuid
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal, SecretReference
from dailyops.deployment import (
    build_deployment_daily_operations_runtime,
    build_web_daily_operations_runtime,
)
from dailyops.runtime import ConnectorBatchResult, DailyOperationsRuntime, build_daily_operations_runtime
from dailyops.services import (
    ingest_csv_text,
    ingest_manual_link,
    propose_daily_analysis,
    run_automatic_collection,
    run_platform_collection,
    start_daily_batch,
)
from integrations.connectors.types import (
    AcquisitionMode,
    ConnectorResult,
    ConnectorRunStatus,
    Platform,
)
from intelligence.models import (
    CollectionRun,
    EvidenceArtifactLink,
    ExternalEvidenceItem,
    RawArtifact,
    SignalAssessment,
)
from products.models import Product


class StaticSevenPlatformRunner:
    def __init__(self, successful: tuple[Platform, ...] = (Platform.PINTEREST, Platform.QUORA)):
        self.successful = successful
        self.calls = 0

    def run(self, requests):
        self.calls += 1
        results = {}
        for platform, request in requests.items():
            if platform in self.successful:
                mode = AcquisitionMode.API if platform is Platform.PINTEREST else AcquisitionMode.BROWSER
                results[platform] = ConnectorResult(
                    platform=platform,
                    status=ConnectorRunStatus.SUCCEEDED,
                    operation_key=request.operation_key,
                    mode=mode,
                    provider="fake-read-only-api" if mode is AcquisitionMode.API else "fake-browser-worker",
                    items=(
                        {
                            "external_id": f"{platform.value.lower()}-1",
                            "url": f"https://example.com/{platform.value.lower()}/1",
                            "title": f"{platform.value} evidence",
                            "content_text": "A fake transport result used only by the test suite.",
                            "attributes": {"fake": True},
                        },
                    ),
                    provenance=({"test_transport": True},),
                )
            else:
                results[platform] = ConnectorResult(
                    platform=platform,
                    status=ConnectorRunStatus.BLOCKED,
                    operation_key=request.operation_key,
                    mode=AcquisitionMode.BROWSER,
                    provider="fake-unpaired-browser-worker",
                    reason="Test browser worker is intentionally not paired",
                )
        return ConnectorBatchResult(results)

    def run_one(self, request):
        return self.run({platform: request for platform in Platform}).results[request.platform]


class NoNetworkTransport:
    def post_json(self, **kwargs):  # pragma: no cover - composition must not call it
        raise AssertionError("composition factory must not call the transport")


def reviewed_test_runtime_factory(settings_values):
    return build_daily_operations_runtime()


class DailyCollectionServiceTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="daily-collector",
            password="safe-local-password-123",
            role=Principal.Role.OWNER,
        )
        self.product = Product.objects.create(
            product_code="PUKO-COLLECT-SERVICE",
            name="PUKO Collection Service",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        now = timezone.now()
        for action in (
            PermissionGrant.Action.MANAGE_ACCOUNT,
            PermissionGrant.Action.COLLECT_READ_ONLY,
            PermissionGrant.Action.EDIT,
        ):
            PermissionGrant.objects.create(
                principal=self.owner,
                scope_kind=(
                    PermissionGrant.ScopeKind.PRODUCT
                    if action == PermissionGrant.Action.EDIT
                    else PermissionGrant.ScopeKind.GLOBAL
                ),
                product=self.product if action == PermissionGrant.Action.EDIT else None,
                action=action,
                risk_level=(
                    PermissionGrant.RiskLevel.HIGH
                    if action == PermissionGrant.Action.MANAGE_ACCOUNT
                    else PermissionGrant.RiskLevel.LOW
                ),
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(days=1),
                granted_by_principal=self.owner,
            )
        from dailyops.services import ensure_default_sources

        ensure_default_sources(principal=self.owner, acting_role=self.owner.role)

    def _batch(self):
        now = timezone.now()
        batch_key = uuid.uuid4()
        start_daily_batch(
            batch_key=batch_key,
            product=self.product,
            query="afternoon focus",
            window_start=now - timedelta(days=7),
            window_end=now,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        return batch_key

    def _deny_platform_collection(self, platform: Platform) -> None:
        now = timezone.now()
        PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PLATFORM,
            platform_code=platform.value,
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            effect=PermissionGrant.Effect.DENY,
            risk_level=PermissionGrant.RiskLevel.LOW,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(days=1),
            granted_by_principal=self.owner,
        )

    def test_platform_connector_is_never_called_before_collection_authorization(self):
        batch_key = self._batch()
        self._deny_platform_collection(Platform.PINTEREST)
        runner = StaticSevenPlatformRunner()
        base_runtime = build_daily_operations_runtime()
        runtime = DailyOperationsRuntime(
            ai_provider=base_runtime.ai_provider,
            connectors=runner,
            live_ai_enabled=False,
            ai_model=base_runtime.ai_model,
        )

        with self.assertRaisesMessage(PermissionDenied, "DENY_GRANT"):
            run_platform_collection(
                batch_key=batch_key,
                product=self.product,
                command_id=uuid.uuid4(),
                platform=Platform.PINTEREST,
                principal=self.owner,
                acting_role=self.owner.role,
                runtime=runtime,
            )

        self.assertEqual(runner.calls, 0)

    def test_seven_platform_connector_is_never_called_when_any_platform_is_denied(self):
        batch_key = self._batch()
        self._deny_platform_collection(Platform.PINTEREST)
        runner = StaticSevenPlatformRunner()
        base_runtime = build_daily_operations_runtime()
        runtime = DailyOperationsRuntime(
            ai_provider=base_runtime.ai_provider,
            connectors=runner,
            live_ai_enabled=False,
            ai_model=base_runtime.ai_model,
        )

        with self.assertRaisesMessage(PermissionDenied, "DENY_GRANT"):
            run_automatic_collection(
                batch_key=batch_key,
                product=self.product,
                command_id=uuid.uuid4(),
                principal=self.owner,
                acting_role=self.owner.role,
                runtime=runtime,
            )

        self.assertEqual(runner.calls, 0)

    def test_default_runtime_records_seven_fail_closed_attempts_and_fallback(self):
        batch_key = self._batch()
        command_id = uuid.uuid4()
        result = run_automatic_collection(
            batch_key=batch_key,
            product=self.product,
            command_id=command_id,
            principal=self.owner,
            acting_role=self.owner.role,
        )

        self.assertTrue(result.created)
        self.assertEqual(len(result.runs), 7)
        self.assertEqual(result.created_count, 0)
        self.assertTrue(
            all(run.result_summary["connector_status"] == "BLOCKED" for run in result.runs)
        )
        self.assertTrue(all(run.result_summary["fallback"] == "CSV_OR_MANUAL" for run in result.runs))
        self.assertEqual(CollectionRun.objects.filter(batch_key=batch_key).count(), 14)

    def test_progressive_collection_persists_one_platform_and_replays_safely(self):
        batch_key = self._batch()
        command_id = uuid.uuid4()
        first = run_platform_collection(
            batch_key=batch_key,
            product=self.product,
            command_id=command_id,
            platform=Platform.PINTEREST,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        replay = run_platform_collection(
            batch_key=batch_key,
            product=self.product,
            command_id=command_id,
            platform=Platform.PINTEREST,
            principal=self.owner,
            acting_role=self.owner.role,
        )

        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(first.run.source.platform_code, Platform.PINTEREST.value)
        self.assertEqual(
            CollectionRun.objects.filter(
                batch_key=batch_key,
                query_spec__automatic_command_id=str(command_id),
            ).count(),
            1,
        )

    def test_fake_api_and_browser_results_persist_provenance_and_replay_without_second_call(self):
        batch_key = self._batch()
        runner = StaticSevenPlatformRunner()
        base_runtime = build_daily_operations_runtime()
        runtime = DailyOperationsRuntime(
            ai_provider=base_runtime.ai_provider,
            connectors=runner,
            live_ai_enabled=False,
            ai_model=base_runtime.ai_model,
        )
        command_id = uuid.uuid4()
        first = run_automatic_collection(
            batch_key=batch_key,
            product=self.product,
            command_id=command_id,
            principal=self.owner,
            acting_role=self.owner.role,
            runtime=runtime,
        )
        replay = run_automatic_collection(
            batch_key=batch_key,
            product=self.product,
            command_id=command_id,
            principal=self.owner,
            acting_role=self.owner.role,
            runtime=runtime,
        )

        self.assertEqual(runner.calls, 1)
        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(first.created_count, 2)
        self.assertEqual(ExternalEvidenceItem.objects.filter(collection_run__batch_key=batch_key).count(), 2)
        self.assertEqual(RawArtifact.objects.filter(collection_run__batch_key=batch_key).count(), 2)
        self.assertEqual(EvidenceArtifactLink.objects.filter(evidence_item__collection_run__batch_key=batch_key).count(), 2)
        self.assertEqual(
            set(first.evidence.values_list("source__source_kind", flat=True))
            if hasattr(first.evidence, "values_list")
            else {item.source.source_kind for item in first.evidence},
            {"THIRD_PARTY_API", "BROWSER"},
        )

    def test_evidence_set_change_creates_new_ai_proposal_version(self):
        batch_key = self._batch()
        first_time = timezone.now()
        ingest_manual_link(
            batch_key=batch_key,
            product=self.product,
            platform=Platform.TIKTOK,
            operation_key=uuid.uuid4(),
            external_url="https://example.com/tiktok/first",
            external_content_id="first",
            title="First evidence",
            content_text="First exact evidence item.",
            collected_at=first_time,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        first = propose_daily_analysis(
            batch_key=batch_key,
            product=self.product,
            principal=self.owner,
            acting_role=self.owner.role,
        )
        ingest_manual_link(
            batch_key=batch_key,
            product=self.product,
            platform=Platform.QUORA,
            operation_key=uuid.uuid4(),
            external_url="https://example.com/quora/second",
            external_content_id="second",
            title="Second evidence",
            content_text="Second exact evidence item.",
            collected_at=first_time + timedelta(seconds=1),
            principal=self.owner,
            acting_role=self.owner.role,
        )
        second = propose_daily_analysis(
            batch_key=batch_key,
            product=self.product,
            principal=self.owner,
            acting_role=self.owner.role,
        )

        self.assertEqual(first.version_number, 1)
        self.assertEqual(second.version_number, 2)
        self.assertEqual(second.supersedes_id, first.pk)
        self.assertEqual(len(first.value["evidence_ids"]), 1)
        self.assertEqual(len(second.value["evidence_ids"]), 2)
        self.assertNotEqual(first.value["evidence_fingerprint"], second.value["evidence_fingerprint"])
        self.assertEqual(
            propose_daily_analysis(
                batch_key=batch_key,
                product=self.product,
                principal=self.owner,
                acting_role=self.owner.role,
            ).pk,
            second.pk,
        )

    def test_more_than_100_evidence_items_fails_closed_before_ai(self):
        batch_key = self._batch()
        collected_at = timezone.now().isoformat()
        rows = ["url,title,collected_at"]
        rows.extend(
            f"https://example.com/item/{index},Evidence {index},{collected_at}"
            for index in range(101)
        )
        ingest_csv_text(
            batch_key=batch_key,
            product=self.product,
            platform=Platform.PINTEREST,
            operation_key=uuid.uuid4(),
            csv_text="\n".join(rows),
            principal=self.owner,
            acting_role=self.owner.role,
        )

        with self.assertRaisesMessage(ValidationError, "at most 100 exact evidence items"):
            propose_daily_analysis(
                batch_key=batch_key,
                product=self.product,
                principal=self.owner,
                acting_role=self.owner.role,
            )
        self.assertFalse(SignalAssessment.objects.exists())


class DeploymentCompositionTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="composition-owner",
            password="safe-local-password-123",
            role=Principal.Role.OWNER,
        )

    def test_default_composition_is_offline_and_connectors_fail_closed(self):
        runtime = build_deployment_daily_operations_runtime(
            {"DEPLOYMENT_STAGE": "LOCAL"}, environment_values={}
        )
        self.assertFalse(runtime.live_ai_enabled)

    def test_connector_enable_refuses_to_guess_routes(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "endpoints are never guessed"):
            build_deployment_daily_operations_runtime(
                {"DEPLOYMENT_STAGE": "STAGING", "DAILYOPS_CONNECTORS_ENABLED": True},
                environment_values={},
            )

    def test_environment_live_flag_is_read_and_missing_live_inputs_fail_closed(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "SecretReference"):
            build_deployment_daily_operations_runtime(
                {},
                environment_values={
                    "DEPLOYMENT_STAGE": "staging-candidate",
                    "DAILYOPS_DEEPSEEK_ENABLED": "true",
                },
            )

    def test_web_composition_loads_an_explicit_reviewed_factory_from_environment(self):
        runtime = build_web_daily_operations_runtime(
            {},
            environment_values={
                "DAILYOPS_RUNTIME_FACTORY": (
                    "dailyops.test_collection_service.reviewed_test_runtime_factory"
                )
            },
        )
        self.assertFalse(runtime.live_ai_enabled)

    def test_live_ai_composition_uses_secret_metadata_without_reading_secret(self):
        reference = SecretReference.objects.create(
            secret_key="deepseek-v4",
            provider_code="deepseek",
            backend=SecretReference.Backend.FILE_MOUNT,
            reference_name="DEEPSEEK_API_KEY",
            environment_scope=SecretReference.EnvironmentScope.STAGING,
            purpose="Daily Operations AI proposals",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        runtime = build_deployment_daily_operations_runtime(
            {
                "DEPLOYMENT_STAGE": "STAGING",
                "DAILYOPS_DEEPSEEK_ENABLED": True,
                "DAILYOPS_DEEPSEEK_MODEL": "deepseek-v4-flash",
                "DEEPSEEK_API_KEY_FILE": "C:/run/secrets/deepseek-api-key",
                "DAILYOPS_DEEPSEEK_INPUT_USD_PER_MILLION": "1.00",
                "DAILYOPS_DEEPSEEK_OUTPUT_USD_PER_MILLION": "2.00",
                "DAILYOPS_DEEPSEEK_MAX_REQUESTS": 3,
                "DAILYOPS_DEEPSEEK_MAX_COST_USD": "0.50",
            },
            ai_secret_reference=reference,
            ai_transport=NoNetworkTransport(),
            environment_values={},
        )
        self.assertTrue(runtime.live_ai_enabled)
        self.assertEqual(runtime.ai_model, "deepseek-v4-flash")

    def test_live_ai_composition_rejects_zero_hosted_api_pricing(self):
        reference = SecretReference.objects.create(
            secret_key="deepseek-v4-zero-price",
            provider_code="deepseek",
            backend=SecretReference.Backend.FILE_MOUNT,
            reference_name="DEEPSEEK_API_KEY",
            environment_scope=SecretReference.EnvironmentScope.STAGING,
            purpose="Reject unpriced hosted requests",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        with self.assertRaisesMessage(ImproperlyConfigured, "greater than zero"):
            build_deployment_daily_operations_runtime(
                {
                    "DEPLOYMENT_STAGE": "STAGING",
                    "DAILYOPS_DEEPSEEK_ENABLED": True,
                    "DEEPSEEK_API_KEY_FILE": "C:/run/secrets/deepseek-api-key",
                    "DAILYOPS_DEEPSEEK_INPUT_USD_PER_MILLION": "0",
                    "DAILYOPS_DEEPSEEK_OUTPUT_USD_PER_MILLION": "2.00",
                    "DAILYOPS_DEEPSEEK_MAX_REQUESTS": 1,
                    "DAILYOPS_DEEPSEEK_MAX_COST_USD": "0.50",
                },
                ai_secret_reference=reference,
                ai_transport=NoNetworkTransport(),
                environment_values={},
            )
