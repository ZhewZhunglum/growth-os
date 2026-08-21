from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from dailyops.schemas import deterministic_analysis
from integrations.ai.budget import BudgetGuard, ModelPricing
from integrations.ai.deepseek import DEEPSEEK_V4_MODELS, DeepSeekV4Config, DeepSeekV4Provider
from integrations.ai.providers import AIProvider, DryRunAIProvider
from integrations.ai.secrets import SecretFileReference
from integrations.ai.transport import DisabledHTTPTransport, HTTPTransport
from integrations.connectors.runtime import ConnectorRuntimeConfig, FallbackConnector, build_connector_registry
from integrations.connectors.types import ConnectorRequest, ConnectorResult, ConnectorRunStatus, Platform
from integrations.errors import IntegrationConfigurationError, IntegrationError


@dataclass(frozen=True, slots=True)
class DeepSeekRuntimeConfig:
    """Explicit live-AI switch; disabled is the safe default."""

    enabled: bool = False
    model: str = "deepseek-v4-flash"
    secret: SecretFileReference | None = None
    pricing: ModelPricing | None = None
    budget: BudgetGuard | None = None
    transport: HTTPTransport | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.model not in DEEPSEEK_V4_MODELS:
            raise IntegrationConfigurationError(
                "Daily Operations supports only deepseek-v4-flash or deepseek-v4-pro"
            )
        if not 0 < self.timeout_seconds <= 120:
            raise IntegrationConfigurationError("DeepSeek timeout must be between 0 and 120 seconds")
        live_values = (self.secret, self.pricing, self.budget, self.transport)
        if not self.enabled and any(value is not None for value in live_values):
            raise IntegrationConfigurationError(
                "Live DeepSeek inputs require enabled=True; disabled mode does not silently retain live credentials"
            )
        if self.enabled:
            if not isinstance(self.secret, SecretFileReference):
                raise IntegrationConfigurationError("Live DeepSeek requires a SecretFileReference")
            if self.pricing is None:
                raise IntegrationConfigurationError("Live DeepSeek requires explicit ModelPricing")
            if self.budget is None:
                raise IntegrationConfigurationError("Live DeepSeek requires an explicit BudgetGuard")
            if self.transport is None or isinstance(self.transport, DisabledHTTPTransport):
                raise IntegrationConfigurationError(
                    "Live DeepSeek requires an explicitly injected enabled HTTP transport"
                )


@dataclass(frozen=True, slots=True)
class DailyOperationsRuntimeConfig:
    ai: DeepSeekRuntimeConfig = field(default_factory=DeepSeekRuntimeConfig)
    connectors: ConnectorRuntimeConfig = field(default_factory=ConnectorRuntimeConfig)
    dry_run_output: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConnectorBatchResult:
    results: Mapping[Platform, ConnectorResult]

    def __post_init__(self) -> None:
        if set(self.results) != set(Platform):
            raise ValueError("A Daily Operations connector batch must contain all seven platforms")
        object.__setattr__(self, "results", MappingProxyType(dict(self.results)))

    @property
    def ready_for_analysis(self) -> bool:
        return any(
            result.status in {ConnectorRunStatus.SUCCEEDED, ConnectorRunStatus.PARTIAL}
            for result in self.results.values()
        )

    @property
    def unresolved_platforms(self) -> tuple[Platform, ...]:
        return tuple(
            platform
            for platform, result in self.results.items()
            if result.status not in {ConnectorRunStatus.SUCCEEDED, ConnectorRunStatus.PARTIAL}
        )


class SevenPlatformConnectorRunner:
    def __init__(self, connectors: Mapping[Platform, FallbackConnector]):
        if set(connectors) != set(Platform):
            raise IntegrationConfigurationError(
                "SevenPlatformConnectorRunner requires exactly the seven V1 platforms"
            )
        self._connectors = MappingProxyType(dict(connectors))

    @property
    def connectors(self) -> Mapping[Platform, FallbackConnector]:
        return self._connectors

    def run_one(self, request: ConnectorRequest) -> ConnectorResult:
        """Run one reviewed connector route for progressive UI collection."""

        platform = request.platform
        if platform not in self._connectors:
            raise IntegrationConfigurationError("The requested platform is not in the V1 connector set")
        try:
            return self._connectors[platform].collect(request)
        except (IntegrationError, OSError, TimeoutError, RuntimeError) as exc:
            return ConnectorResult(
                platform=platform,
                status=ConnectorRunStatus.FAILED,
                operation_key=request.operation_key,
                mode=None,
                provider=None,
                reason=f"Connector execution failed safely: {type(exc).__name__}",
                retryable=True,
            )

    def run(self, requests: Mapping[Platform, ConnectorRequest]) -> ConnectorBatchResult:
        if set(requests) != set(Platform):
            raise IntegrationConfigurationError(
                "Daily Operations collection requires exactly one request for each V1 platform"
            )
        results: dict[Platform, ConnectorResult] = {}
        for platform in Platform:
            request = requests[platform]
            if request.platform is not platform:
                raise IntegrationConfigurationError(
                    f"Connector request key {platform.value} does not match its request platform"
                )
            results[platform] = self.run_one(request)
        return ConnectorBatchResult(results)


@dataclass(frozen=True, slots=True)
class DailyOperationsRuntime:
    ai_provider: AIProvider
    connectors: SevenPlatformConnectorRunner
    live_ai_enabled: bool
    ai_model: str


def build_daily_operations_runtime(
    config: DailyOperationsRuntimeConfig | None = None,
) -> DailyOperationsRuntime:
    """Build the only supported Daily Operations execution bundle.

    No Django settings or environment variables are read here.  The composition
    root must pass explicit, already-reviewed configuration.  With no arguments,
    AI is deterministic dry-run and every network/browser route is fail-closed.
    """

    config = config or DailyOperationsRuntimeConfig()
    if config.ai.enabled:
        assert config.ai.secret is not None
        assert config.ai.pricing is not None
        assert config.ai.budget is not None
        assert config.ai.transport is not None
        ai_provider: AIProvider = DeepSeekV4Provider(
            DeepSeekV4Config(
                secret=config.ai.secret,
                model=config.ai.model,
                timeout_seconds=config.ai.timeout_seconds,
                pricing=config.ai.pricing,
            ),
            budget=config.ai.budget,
            transport=config.ai.transport,
        )
    else:
        output = (
            config.dry_run_output
            if config.dry_run_output is not None
            else deterministic_analysis(
                query="Daily Operations dry run",
                evidence_count=0,
                first_title="",
            )
        )
        ai_provider = DryRunAIProvider(output, model="daily-operations-dry-run")

    return DailyOperationsRuntime(
        ai_provider=ai_provider,
        connectors=SevenPlatformConnectorRunner(build_connector_registry(config.connectors)),
        live_ai_enabled=config.ai.enabled,
        ai_model=config.ai.model if config.ai.enabled else "daily-operations-dry-run",
    )


__all__ = [
    "ConnectorBatchResult",
    "DailyOperationsRuntime",
    "DailyOperationsRuntimeConfig",
    "DeepSeekRuntimeConfig",
    "SevenPlatformConnectorRunner",
    "build_daily_operations_runtime",
]
