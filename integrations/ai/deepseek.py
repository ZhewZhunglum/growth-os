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
        cached_input_tokens = _nonnegative_int(
            usage_payload.get("prompt_cache_hit_tokens", 0), "prompt_cache_hit_tokens"
        )
        cache_miss_value = usage_payload.get("prompt_cache_miss_tokens")
        if cache_miss_value is not None:
            cache_miss_tokens = _nonnegative_int(cache_miss_value, "prompt_cache_miss_tokens")
            if cached_input_tokens + cache_miss_tokens != input_tokens:
                raise ProviderResponseError(
                    "DeepSeek cache hit and miss tokens must equal prompt_tokens"
                )
        if cached_input_tokens > input_tokens:
            raise ProviderResponseError(
                "DeepSeek usage.prompt_cache_hit_tokens cannot exceed prompt_tokens"
            )
        total_tokens = _nonnegative_int(
            usage_payload.get("total_tokens", input_tokens + output_tokens), "total_tokens"
        )
        actual_cost = self.config.pricing.estimate(
            input_tokens, output_tokens, cached_input_tokens=cached_input_tokens
        )
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
                cached_input_tokens=cached_input_tokens,
            ),
            provider_request_id=response.request_id or _optional_string(response.payload.get("id")),
        )


def _openai_payload(model: str, request: AIRequest) -> dict[str, Any]:
    messages = [{"role": item.role, "content": item.content} for item in request.messages]
    messages.insert(0, {"role": "system", "content": _json_output_instruction(request)})
    return {
        "model": model,
        "messages": messages,
        # V4 enables thinking by default. Structured JSON generation uses the
        # non-thinking mode so temperature is effective and no hidden reasoning
        # tokens consume the bounded output allowance.
        "thinking": {"type": "disabled"},
        "temperature": request.temperature,
        "max_tokens": request.max_output_tokens,
        # DeepSeek Chat Completions supports JSON Object mode, not OpenAI's
        # response_format.json_schema envelope. The returned object is checked
        # against the exact schema locally before it can enter the application.
        "response_format": {"type": "json_object"},
        "stream": False,
        # DeepSeek's user_id accepts only a restricted character set and must
        # not contain private user data. A fingerprint is stable, opaque and
        # supports provider-side cache/scheduling isolation.
        "user_id": f"growth-os-{request.fingerprint[:40]}",
    }


def _parse_structured_output(payload: Mapping[str, Any], request: AIRequest) -> dict[str, Any]:
    try:
        choice = payload["choices"][0]
        finish_reason = choice["finish_reason"]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderResponseError(
            "DeepSeek response is missing choices[0] completion fields"
        ) from exc
    if finish_reason != "stop":
        reasons = {
            "length": "DeepSeek JSON output was truncated by the token limit",
            "content_filter": "DeepSeek JSON output was blocked by content filtering",
            "tool_calls": "DeepSeek unexpectedly returned a tool call for a JSON-only request",
            "insufficient_system_resource": "DeepSeek interrupted generation due to insufficient resources",
        }
        raise ProviderResponseError(
            reasons.get(str(finish_reason), f"DeepSeek returned unsupported finish_reason {finish_reason!r}")
        )
    if not isinstance(content, str):
        raise ProviderResponseError("DeepSeek message content must be a JSON string")
    if not content.strip():
        raise StructuredOutputError("DeepSeek returned empty JSON content")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError("DeepSeek returned non-JSON structured output") from exc
    if not isinstance(value, dict):
        raise StructuredOutputError("DeepSeek structured output root must be an object")
    validate_json_schema(value, request.output.schema)
    return value


def _json_output_instruction(request: AIRequest) -> str:
    schema = dict(request.output.schema)
    example = _example_for_schema(schema)
    return (
        "Return exactly one valid JSON object and no markdown or commentary. "
        f"The JSON object must conform to this JSON Schema named {request.output.name!r}: "
        f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}. "
        "Example JSON output shape (replace illustrative values with the requested result): "
        f"{json.dumps(example, ensure_ascii=False, separators=(',', ':'))}"
    )


def _example_for_schema(schema: Mapping[str, Any]) -> Any:
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    expected = schema.get("type")
    if isinstance(expected, list):
        expected = next((item for item in expected if item != "null"), "null")
    if expected == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return {}
        required = schema.get("required", [])
        return {
            key: _example_for_schema(child)
            for key, child in properties.items()
            if key in required and isinstance(child, Mapping)
        }
    if expected == "array":
        minimum = schema.get("minItems", 0)
        count = minimum if isinstance(minimum, int) and minimum > 0 else 1
        maximum = schema.get("maxItems")
        if isinstance(maximum, int):
            count = min(count, maximum)
        item_schema = schema.get("items", {})
        return [_example_for_schema(item_schema) for _ in range(count)] if isinstance(item_schema, Mapping) else []
    if expected == "string":
        minimum = schema.get("minLength", 0)
        return "x" * max(1, minimum if isinstance(minimum, int) else 0)
    if expected == "integer":
        minimum = schema.get("minimum", 0)
        return math.ceil(minimum) if isinstance(minimum, (int, float)) else 0
    if expected == "number":
        minimum = schema.get("minimum", 0)
        return minimum if isinstance(minimum, (int, float)) else 0
    if expected == "boolean":
        return False
    return None


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
