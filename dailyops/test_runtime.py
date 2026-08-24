from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from dailyops.runtime import (
    DailyOperationsRuntimeConfig,
    DeepSeekRuntimeConfig,
    build_daily_operations_runtime,
)
from dailyops.schemas import DAILY_ANALYSIS_SCHEMA, deterministic_analysis
from integrations.ai.budget import BudgetGuard, BudgetLimits, ModelPricing
from integrations.ai.secrets import SecretFileReference
from integrations.ai.transport import TransportResponse
from integrations.ai.types import AIExecutionStatus, AIMessage, AIRequest, StructuredOutputSpec
from integrations.browser_worker import (
    BrowserWorkerJob,
    BrowserWorkerJobStatus,
    BrowserWorkerPairing,
    BrowserWorkerResult,
)
from integrations.connectors.runtime import (
    BrowserRouteConfig,
    ConnectorRuntimeConfig,
)
from integrations.connectors.types import ConnectorRequest, ConnectorRunStatus, Platform
from integrations.errors import IntegrationConfigurationError


NOW = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)


def connector_requests() -> dict[Platform, ConnectorRequest]:
    return {
        platform: ConnectorRequest(
            platform=platform,
            operation_key=f"daily:2026-08-21:{platform.value.lower()}",
            query="focus supplements",
            window_start=NOW - timedelta(days=1),
            window_end=NOW,
            market_code="US",
            language_code="en",
        )
        for platform in Platform
    }


def ai_request() -> AIRequest:
    return AIRequest(
        messages=(AIMessage(role="user", content="Analyze the collected demand evidence."),),
        output=StructuredOutputSpec(name="daily_analysis", schema=DAILY_ANALYSIS_SCHEMA),
        operation_key="daily:2026-08-21:analysis",
    )


class FakeAITransport:
    def __init__(self, output: Mapping[str, Any]):
        self.output = dict(output)
        self.calls: list[dict[str, Any]] = []

    def post_json(self, **kwargs: Any) -> TransportResponse:
        self.calls.append(kwargs)
        return TransportResponse(
            status_code=200,
            payload={
                "id": "deepseek-test-1",
                "choices": [{"message": {"content": json.dumps(self.output)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
            },
            request_id="deepseek-test-1",
        )


class FakeBrowserClient:
    def __init__(self):
        self.jobs: list[BrowserWorkerJob] = []

    def submit(self, job: BrowserWorkerJob) -> None:
        self.jobs.append(job)

    def result(self, job_id: uuid.UUID) -> BrowserWorkerResult | None:
        job = self.jobs[-1]
        return BrowserWorkerResult(
            job_id=job_id,
            operation_key=job.operation_key,
            job_fingerprint=job.fingerprint,
            status=BrowserWorkerJobStatus.SUCCEEDED,
            completed_at=NOW,
            items=({"external_id": "quora-1", "title": "Quora research result"},),
        )

    def cancel(self, job_id: uuid.UUID, reason: str) -> None:
        return None


class DailyOperationsRuntimeTests(unittest.TestCase):
    def test_default_runtime_is_dry_run_and_all_external_routes_fail_closed(self):
        runtime = build_daily_operations_runtime()
        analysis = runtime.ai_provider.generate(ai_request())
        batch = runtime.connectors.run(connector_requests())

        self.assertFalse(runtime.live_ai_enabled)
        self.assertEqual(analysis.status, AIExecutionStatus.DRY_RUN)
        self.assertEqual(analysis.provider, "dry-run")
        self.assertEqual(set(batch.results), set(Platform))
        self.assertTrue(all(result.status is ConnectorRunStatus.BLOCKED for result in batch.results.values()))
        self.assertFalse(batch.ready_for_analysis)

    def test_live_inputs_are_rejected_without_explicit_enable(self):
        with self.assertRaises(IntegrationConfigurationError):
            DeepSeekRuntimeConfig(
                secret=SecretFileReference(Path("not-read.secret")),
            )

    def test_live_enable_requires_secret_pricing_budget_and_transport(self):
        with self.assertRaises(IntegrationConfigurationError):
            DeepSeekRuntimeConfig(enabled=True)
        with self.assertRaises(IntegrationConfigurationError):
            DeepSeekRuntimeConfig(enabled=True, model="not-deepseek")

    def test_explicit_live_deepseek_flash_uses_secret_pricing_and_budget(self):
        output = deterministic_analysis(query="focus", evidence_count=3, first_title="Focus")
        transport = FakeAITransport(output)
        budget = BudgetGuard(BudgetLimits(max_requests=1, max_cost_usd=Decimal("0.10")))
        pricing = ModelPricing(
            input_usd_per_million_tokens=Decimal("1"),
            output_usd_per_million_tokens=Decimal("2"),
        )
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "deepseek.secret"
            secret.write_text("test-only-key", encoding="utf-8")
            runtime = build_daily_operations_runtime(
                DailyOperationsRuntimeConfig(
                    ai=DeepSeekRuntimeConfig(
                        enabled=True,
                        model="deepseek-v4-flash",
                        secret=SecretFileReference(secret),
                        pricing=pricing,
                        budget=budget,
                        transport=transport,
                    )
                )
            )
            result = runtime.ai_provider.generate(ai_request())

        self.assertTrue(runtime.live_ai_enabled)
        self.assertEqual(runtime.ai_model, "deepseek-v4-flash")
        self.assertEqual(result.status, AIExecutionStatus.SUCCEEDED)
        self.assertEqual(transport.calls[0]["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(budget.snapshot().requests_used, 1)

    def test_pro_model_is_allowed_but_other_models_are_not(self):
        with self.assertRaises(IntegrationConfigurationError):
            DeepSeekRuntimeConfig(model="deepseek-chat")
        self.assertEqual(DeepSeekRuntimeConfig(model="deepseek-v4-pro").model, "deepseek-v4-pro")

    def test_injected_browser_runner_can_complete_quora_while_others_stay_blocked(self):
        pairing = BrowserWorkerPairing(
            pairing_id=uuid.uuid4(),
            worker_id="worker-1",
            dedicated_profile_id="quora-profile",
            dedicated_profile_label="Growth OS Quora",
            browser_family="chromium",
            paired_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(days=1),
            capabilities=(Platform.QUORA.value,),
        )
        browser = FakeBrowserClient()
        runtime = build_daily_operations_runtime(
            DailyOperationsRuntimeConfig(
                connectors=ConnectorRuntimeConfig(
                    browser_routes={
                        Platform.QUORA: BrowserRouteConfig(
                            platform=Platform.QUORA,
                            provider="quora-browser-worker",
                            allowed_hosts=("quora.com",),
                            pairing=pairing,
                        )
                    },
                    browser_clients={Platform.QUORA: browser},
                    browser_clocks={Platform.QUORA: lambda: NOW},
                )
            )
        )
        batch = runtime.connectors.run(connector_requests())
        self.assertEqual(batch.results[Platform.QUORA].status, ConnectorRunStatus.SUCCEEDED)
        self.assertEqual(len(batch.unresolved_platforms), 6)
        self.assertTrue(batch.ready_for_analysis)
        self.assertEqual(len(browser.jobs), 1)

    def test_runner_refuses_partial_or_mismatched_platform_request_sets(self):
        runtime = build_daily_operations_runtime()
        requests = connector_requests()
        requests.pop(Platform.GOOGLE_ANALYTICS_4)
        with self.assertRaises(IntegrationConfigurationError):
            runtime.connectors.run(requests)


if __name__ == "__main__":
    unittest.main()
