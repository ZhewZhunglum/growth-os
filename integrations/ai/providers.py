from __future__ import annotations

from collections import deque
from copy import deepcopy
from typing import Any, Mapping, Protocol

from integrations.ai.json_schema import validate_json_schema
from integrations.ai.types import AIExecutionStatus, AIRequest, AIResult, AIUsage


class AIProvider(Protocol):
    def generate(self, request: AIRequest) -> AIResult: ...


class DryRunAIProvider:
    """Deterministic provider that never performs external work."""

    def __init__(self, output: Mapping[str, Any], *, model: str = "dry-run"):
        self._output = deepcopy(dict(output))
        self._model = model

    def generate(self, request: AIRequest) -> AIResult:
        validate_json_schema(self._output, request.output.schema)
        return AIResult(
            status=AIExecutionStatus.DRY_RUN,
            provider="dry-run",
            model=self._model,
            operation_key=request.operation_key,
            request_fingerprint=request.fingerprint,
            output=deepcopy(self._output),
            usage=AIUsage(input_tokens=0, output_tokens=0, total_tokens=0, estimated_cost_usd=0.0),
        )


class FakeAIProvider:
    """Queue-backed fake intended for tests and local deterministic fixtures."""

    def __init__(self, outputs: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]):
        self._outputs = deque(deepcopy([dict(item) for item in outputs]))
        self.requests: list[AIRequest] = []

    def generate(self, request: AIRequest) -> AIResult:
        if not self._outputs:
            raise AssertionError("FakeAIProvider has no queued output")
        self.requests.append(request)
        output = self._outputs.popleft()
        validate_json_schema(output, request.output.schema)
        return AIResult(
            status=AIExecutionStatus.SUCCEEDED,
            provider="fake",
            model="fake",
            operation_key=request.operation_key,
            request_fingerprint=request.fingerprint,
            output=output,
            usage=AIUsage(input_tokens=1, output_tokens=1, total_tokens=2, estimated_cost_usd=0.0),
            provider_request_id="fake-request",
        )
