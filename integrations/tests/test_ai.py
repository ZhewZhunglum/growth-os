from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from integrations.ai import (
    AIExecutionStatus,
    AIMessage,
    AIRequest,
    BudgetGuard,
    BudgetLimits,
    DeepSeekV4Config,
    DeepSeekV4Provider,
    DisabledHTTPTransport,
    DryRunAIProvider,
    ModelPricing,
    SecretFileReference,
    StructuredOutputSpec,
    TransportResponse,
)
from integrations.errors import (
    BudgetExceeded,
    IntegrationConfigurationError,
    NetworkAccessDisabled,
    StructuredOutputError,
)


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["answer", "score"],
    "additionalProperties": False,
}


class RecordingTransport:
    def __init__(self, output: Mapping[str, Any] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.output = output or {"answer": "ok", "score": 1}

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": dict(payload), "timeout": timeout_seconds}
        )
        return TransportResponse(
            status_code=200,
            payload={
                "id": "request-1",
                "choices": [{"message": {"content": json.dumps(self.output)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )


def request() -> AIRequest:
    return AIRequest(
        messages=(AIMessage(role="system", content="Return evidence."),),
        output=StructuredOutputSpec(name="evidence", schema=SCHEMA),
        operation_key="daily:2026-08-21:ai-1",
        max_output_tokens=100,
    )


class DeepSeekAdapterTests(unittest.TestCase):
    def test_default_model_is_flash_and_network_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "key"
            secret_path.write_text("not-a-real-key", encoding="utf-8")
            config = DeepSeekV4Config(
                secret=SecretFileReference(secret_path),
                pricing=ModelPricing(Decimal("0"), Decimal("0")),
            )
            provider = DeepSeekV4Provider(
                config,
                budget=BudgetGuard(BudgetLimits(max_requests=1, max_cost_usd=Decimal("0"))),
            )
            self.assertEqual(config.model, "deepseek-v4-flash")
            with self.assertRaises(NetworkAccessDisabled):
                provider.generate(request())

    def test_openai_compatible_payload_and_structured_result(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "key"
            secret_path.write_text("test-key", encoding="utf-8")
            transport = RecordingTransport()
            provider = DeepSeekV4Provider(
                DeepSeekV4Config(
                    secret=SecretFileReference(secret_path),
                    model="deepseek-v4-pro",
                    pricing=ModelPricing(Decimal("0.10"), Decimal("0.20")),
                ),
                budget=BudgetGuard(BudgetLimits(max_requests=2, max_cost_usd=Decimal("1"))),
                transport=transport,
            )
            result = provider.generate(request())
            self.assertEqual(result.status, AIExecutionStatus.SUCCEEDED)
            self.assertEqual(result.output["answer"], "ok")
            self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer test-key")
            payload = transport.calls[0]["payload"]
            self.assertEqual(payload["model"], "deepseek-v4-pro")
            self.assertEqual(payload["response_format"]["type"], "json_schema")
            self.assertFalse(payload["stream"])

    def test_live_adapter_requires_explicit_pricing_and_budget(self):
        config = DeepSeekV4Config(secret=SecretFileReference(Path("missing")))
        with self.assertRaises(IntegrationConfigurationError):
            DeepSeekV4Provider(config).generate(request())

    def test_budget_fails_closed_before_second_request(self):
        guard = BudgetGuard(BudgetLimits(max_requests=1, max_cost_usd=Decimal("1")))
        guard.consume(Decimal("0.1"))
        with self.assertRaises(BudgetExceeded):
            guard.consume(Decimal("0.1"))

    def test_cost_budget_fails_closed_before_transport(self):
        guard = BudgetGuard(BudgetLimits(max_requests=2, max_cost_usd=Decimal("0.01")))
        with self.assertRaises(BudgetExceeded):
            guard.consume(Decimal("0.02"))

    def test_structured_output_schema_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "key"
            secret_path.write_text("test-key", encoding="utf-8")
            provider = DeepSeekV4Provider(
                DeepSeekV4Config(
                    secret=SecretFileReference(secret_path),
                    pricing=ModelPricing(Decimal("0"), Decimal("0")),
                ),
                budget=BudgetGuard(BudgetLimits(max_requests=1, max_cost_usd=Decimal("0"))),
                transport=RecordingTransport({"answer": "missing score"}),
            )
            with self.assertRaises(StructuredOutputError):
                provider.generate(request())

    def test_dry_run_never_needs_a_secret_or_transport(self):
        result = DryRunAIProvider({"answer": "fixture", "score": 3}).generate(request())
        self.assertEqual(result.status, AIExecutionStatus.DRY_RUN)
        self.assertEqual(result.usage.total_tokens, 0)

    def test_disabled_transport_never_performs_io(self):
        with self.assertRaises(NetworkAccessDisabled):
            DisabledHTTPTransport().post_json(url="https://example.invalid", headers={}, payload={}, timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
