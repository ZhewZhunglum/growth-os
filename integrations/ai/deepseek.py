from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from integrations.ai.budget import BudgetGuard, ModelPricing
from integrations.ai.json_schema import validate_json_schema
from integrations.ai.secrets import SecretFileReference, read_secret_file
from integrations.ai.transport import DisabledHTTPTransport, HTTPTransport
from integrations.ai.types import AIExecutionStatus, AIRequest, AIResult, AIUsage
from integrations.errors import IntegrationConfigurationError, ProviderResponseError, StructuredOutputError


DEEPSEEK_V4_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})


@dataclass(frozen=True, slots=True)
class DeepSeekV4Config:
    secret: SecretFileReference
    model: str = "deepseek-v4-flash"
    endpoint: str = "https://api.deepseek.com/chat/completions"
    timeout_seconds: float = 30.0
    pricing: ModelPricing | None = None
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        if self.model not in DEEPSEEK_V4_MODELS:
            raise IntegrationConfigurationError(
                f"Unsupported DeepSeek V4 model {self.model!r}; use deepseek-v4-flash or deepseek-v4-pro"
            )
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in ({"https", "http"} if self.allow_insecure_http else {"https"}):
            raise IntegrationConfigurationError("DeepSeek endpoint must use HTTPS")
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise IntegrationConfigurationError("DeepSeek endpoint URL is invalid")
        if not 0 < self.timeout_seconds <= 120:
            raise IntegrationConfigurationError("DeepSeek timeout must be between 0 and 120 seconds")


class DeepSeekV4Provider:
    """OpenAI-compatible DeepSeek V4 adapter.

    A disabled transport is used by default.  A live call additionally requires
    explicit model pricing and a BudgetGuard supplied by the caller; the adapter
    never assumes that an open-source model implies a free hosted API.
    """

    provider_name = "deepseek"

    def __init__(
        self,
        config: DeepSeekV4Config,
        *,
        budget: BudgetGuard | None = None,
        transport: HTTPTransport | None = None,
    ):
        self.config = config
        self.budget = budget
        self.transport = transport or DisabledHTTPTransport()

    def generate(self, request: AIRequest) -> AIResult:
        if isinstance(self.transport, DisabledHTTPTransport):
            # Fail before reading a secret or consuming budget.  This is the
            # normal default path until a caller explicitly injects a transport.
            self.transport.post_json(
                url=self.config.endpoint,
                headers={},
                payload={},
                timeout_seconds=self.config.timeout_seconds,
            )
        if self.config.pricing is None:
            raise IntegrationConfigurationError(
                "Explicit DeepSeek pricing is required before live execution; configure zero only when verified"
            )
        if self.budget is None:
            raise IntegrationConfigurationError("A BudgetGuard is required before live DeepSeek execution")

        estimated_input = _estimate_input_tokens(request)
        estimated_cost = self.config.pricing.estimate(estimated_input, request.max_output_tokens)
        self.budget.consume(estimated_cost)
        api_key = read_secret_file(self.config.secret)
        payload = _openai_payload(self.config.model, request)
        response = self.transport.post_json(
            url=self.config.endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            payload=payload,
            timeout_seconds=self.config.timeout_seconds,
        )
        if not 200 <= response.status_code < 300:
            raise ProviderResponseError(f"DeepSeek returned HTTP {response.status_code}")

        output = _parse_structured_output(response.payload, request)
        usage_payload = response.payload.get("usage", {})
        input_tokens = _nonnegative_int(usage_payload.get("prompt_tokens", 0), "prompt_tokens")
        output_tokens = _nonnegative_int(usage_payload.get("completion_tokens", 0), "completion_tokens")
        total_tokens = _nonnegative_int(
            usage_payload.get("total_tokens", input_tokens + output_tokens), "total_tokens"
        )
        actual_cost = self.config.pricing.estimate(input_tokens, output_tokens)
        return AIResult(
            status=AIExecutionStatus.SUCCEEDED,
            provider=self.provider_name,
            model=self.config.model,
            operation_key=request.operation_key,
            request_fingerprint=request.fingerprint,
            output=output,
            usage=AIUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=float(actual_cost),
            ),
            provider_request_id=response.request_id or _optional_string(response.payload.get("id")),
        )


def _openai_payload(model: str, request: AIRequest) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": item.role, "content": item.content} for item in request.messages],
        "temperature": request.temperature,
        "max_tokens": request.max_output_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": request.output.name,
                "strict": request.output.strict,
                "schema": dict(request.output.schema),
            },
        },
        "stream": False,
        "user": request.operation_key,
    }


def _parse_structured_output(payload: Mapping[str, Any], request: AIRequest) -> dict[str, Any]:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderResponseError("DeepSeek response is missing choices[0].message.content") from exc
    if not isinstance(content, str):
        raise ProviderResponseError("DeepSeek message content must be a JSON string")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError("DeepSeek returned non-JSON structured output") from exc
    if not isinstance(value, dict):
        raise StructuredOutputError("DeepSeek structured output root must be an object")
    validate_json_schema(value, request.output.schema)
    return value


def _estimate_input_tokens(request: AIRequest) -> int:
    characters = sum(len(item.role) + len(item.content) for item in request.messages)
    characters += len(json.dumps(dict(request.output.schema), ensure_ascii=False))
    return max(1, math.ceil(characters / 4))


def _nonnegative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderResponseError(f"DeepSeek usage.{name} must be a non-negative integer")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
