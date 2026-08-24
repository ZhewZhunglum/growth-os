from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from integrations.connectors.types import AvailabilityState, Platform
from integrations.errors import NetworkAccessDisabled

from .catalog import default_publication_catalog
from .types import (
    PublicationDispatchRequest,
    PublicationDispatchResult,
    PublicationDispatchStatus,
    PublicationMode,
    PublicationTransport,
)


class DisabledPublicationTransport:
    def dispatch(self, request: PublicationDispatchRequest) -> PublicationDispatchResult:
        raise NetworkAccessDisabled(
            "Publication networking is disabled; inject an explicitly enabled transport"
        )


class DryRunPublicationTransport:
    """Offline preview transport.  It never creates external publication proof."""

    def __init__(self, provider: str = "publication-dry-run"):
        self.provider = provider

    def dispatch(self, request: PublicationDispatchRequest) -> PublicationDispatchResult:
        return PublicationDispatchResult(
            platform=request.platform,
            mode=request.mode,
            provider=self.provider,
            status=PublicationDispatchStatus.DRY_RUN,
            operation_key=request.operation_key,
            reason="Dry run only; no external platform was changed",
        )


@dataclass(frozen=True, slots=True)
class PublicationRuntimeConfig:
    transports: Mapping[tuple[Platform, PublicationMode], PublicationTransport] = field(
        default_factory=dict
    )


class PublicationRuntime:
    """Explicit, fail-closed mutating connector registry.

    Unlike read-only collection, publication never silently falls through from
    one mode to another after a failure.  The confirmed API/BROWSER/MANUAL mode
    is part of the human approval snapshot.
    """

    def __init__(self, config: PublicationRuntimeConfig | None = None):
        self.config = config or PublicationRuntimeConfig()
        self.catalog = default_publication_catalog()

    def dispatch(self, request: PublicationDispatchRequest) -> PublicationDispatchResult:
        descriptor = self.catalog[request.platform].route(request.mode)
        if request.mode is PublicationMode.MANUAL:
            return PublicationDispatchResult(
                platform=request.platform,
                mode=request.mode,
                provider=descriptor.provider,
                status=PublicationDispatchStatus.BLOCKED,
                operation_key=request.operation_key,
                reason="Manual mode requires external URL/content-ID proof, not a transport call",
            )
        if descriptor.state is AvailabilityState.UNAVAILABLE:
            return PublicationDispatchResult(
                platform=request.platform,
                mode=request.mode,
                provider=descriptor.provider,
                status=PublicationDispatchStatus.UNAVAILABLE,
                operation_key=request.operation_key,
                reason=descriptor.reason,
            )
        transport = self.config.transports.get((request.platform, request.mode))
        if transport is None:
            return PublicationDispatchResult(
                platform=request.platform,
                mode=request.mode,
                provider=descriptor.provider,
                status=PublicationDispatchStatus.BLOCKED,
                operation_key=request.operation_key,
                reason="The confirmed publication transport is not configured",
            )
        try:
            result = transport.dispatch(request)
        except (NetworkAccessDisabled, OSError, TimeoutError, RuntimeError) as exc:
            return PublicationDispatchResult(
                platform=request.platform,
                mode=request.mode,
                provider=descriptor.provider,
                status=PublicationDispatchStatus.FAILED,
                operation_key=request.operation_key,
                reason=str(exc),
            )
        if (
            result.platform is not request.platform
            or result.mode is not request.mode
            or result.operation_key != request.operation_key
        ):
            return PublicationDispatchResult(
                platform=request.platform,
                mode=request.mode,
                provider=descriptor.provider,
                status=PublicationDispatchStatus.FAILED,
                operation_key=request.operation_key,
                reason="Publisher response does not bind the exact confirmed request",
            )
        return result
