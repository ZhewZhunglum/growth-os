from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class AIExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    DRY_RUN = "DRY_RUN"


_MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})
_SCHEMA_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class AIMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in _MESSAGE_ROLES:
            raise ValueError(f"Unsupported AI message role: {self.role!r}")
        if not self.content or not self.content.strip():
            raise ValueError("AI message content must not be blank")
        if len(self.content) > 500_000:
            raise ValueError("AI message content exceeds the 500,000 character safety limit")


@dataclass(frozen=True, slots=True)
class StructuredOutputSpec:
    name: str
    schema: Mapping[str, Any]
    strict: bool = True

    def __post_init__(self) -> None:
        if not _SCHEMA_NAME.fullmatch(self.name):
            raise ValueError("Structured output name must be a safe 1-64 character identifier")
        if self.schema.get("type") != "object":
            raise ValueError("Structured AI output must have an object JSON schema root")
        object.__setattr__(self, "schema", MappingProxyType(dict(self.schema)))


@dataclass(frozen=True, slots=True)
class AIRequest:
    messages: tuple[AIMessage, ...]
    output: StructuredOutputSpec
    operation_key: str
    max_output_tokens: int = 2_048
    temperature: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("At least one AI message is required")
        if not self.operation_key or len(self.operation_key) > 200:
            raise ValueError("operation_key must contain 1-200 characters")
        if not 1 <= self.max_output_tokens <= 32_768:
            raise ValueError("max_output_tokens must be between 1 and 32768")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be between 0 and 2")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def fingerprint(self) -> str:
        canonical = {
            "messages": [{"role": item.role, "content": item.content} for item in self.messages],
            "output": {"name": self.output.name, "schema": dict(self.output.schema)},
            "operation_key": self.operation_key,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "metadata": dict(self.metadata),
        }
        payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AIUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float | None = None

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.total_tokens) < 0:
            raise ValueError("Token counts cannot be negative")
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens cannot be smaller than input + output tokens")


@dataclass(frozen=True, slots=True)
class AIResult:
    status: AIExecutionStatus
    provider: str
    model: str
    operation_key: str
    request_fingerprint: str
    output: Mapping[str, Any]
    usage: AIUsage
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", MappingProxyType(dict(self.output)))
