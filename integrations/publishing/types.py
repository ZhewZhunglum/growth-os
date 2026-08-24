from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from integrations.connectors.types import AvailabilityState, Platform


class PublicationMode(StrEnum):
    API = "API"
    BROWSER = "BROWSER"
    MANUAL = "MANUAL"


class PublicationAssetRepresentation(StrEnum):
    """Exact form of the immutable asset sent to a publication route."""

    EXTERNAL_URL = "EXTERNAL_URL"
    INLINE_TEXT = "INLINE_TEXT"


class PublicationDispatchStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    DRY_RUN = "DRY_RUN"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PublicationRouteAvailability:
    mode: PublicationMode
    state: AvailabilityState
    provider: str
    priority: int
    reason: str

    def __post_init__(self) -> None:
        if not self.provider or not self.reason:
            raise ValueError("A publication route requires a provider and reason")
        if self.priority < 1:
            raise ValueError("Publication route priority must be positive")


@dataclass(frozen=True, slots=True)
class PublicationConnectorDescriptor:
    platform: Platform
    routes: tuple[PublicationRouteAvailability, ...]

    def __post_init__(self) -> None:
        modes = [route.mode for route in self.routes]
        if set(modes) != set(PublicationMode) or len(modes) != len(PublicationMode):
            raise ValueError("Every platform must declare API, BROWSER, and MANUAL exactly once")
        priorities = [route.priority for route in self.routes]
        if len(priorities) != len(set(priorities)):
            raise ValueError("Publication route priorities must be unique")
        object.__setattr__(self, "routes", tuple(sorted(self.routes, key=lambda item: item.priority)))

    def route(self, mode: PublicationMode) -> PublicationRouteAvailability:
        return next(route for route in self.routes if route.mode is mode)


_OPERATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


@dataclass(frozen=True, slots=True)
class PublicationDispatchRequest:
    """Exact immutable envelope passed to an explicitly injected publisher.

    Implementations must treat ``operation_key`` as an idempotency key.  This
    is important because an external platform may accept a request immediately
    before the application process loses its response.
    """

    platform: Platform
    mode: PublicationMode
    operation_key: str
    account_ref: str
    asset_version_id: str
    asset_representation_kind: PublicationAssetRepresentation
    asset_external_url: str
    asset_inline_content: str
    gate_id: str
    gate_context_sha256: str
    human_confirmation_id: str
    confirmed_by_principal_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.platform, Platform) or not isinstance(self.mode, PublicationMode):
            raise ValueError("Publication platform and mode must use the declared V1 enums")
        try:
            representation_kind = PublicationAssetRepresentation(self.asset_representation_kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("Publication asset representation is unsupported") from exc
        object.__setattr__(self, "asset_representation_kind", representation_kind)
        if not _OPERATION_KEY.fullmatch(self.operation_key):
            raise ValueError("operation_key has an invalid format")
        if not all(
            value.strip()
            for value in (
                self.account_ref,
                self.asset_version_id,
                self.gate_id,
                self.gate_context_sha256,
                self.human_confirmation_id,
                self.confirmed_by_principal_id,
            )
        ):
            raise ValueError("Publication dispatch references must be non-empty")
        if representation_kind is PublicationAssetRepresentation.EXTERNAL_URL:
            parsed = urlsplit(self.asset_external_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("An external publication asset requires an HTTP(S) link")
            if parsed.username or parsed.password:
                raise ValueError("Asset links must not embed credentials")
            if self.asset_inline_content:
                raise ValueError("An external publication asset cannot also contain inline text")
        else:
            if self.asset_external_url:
                raise ValueError("An inline publication asset cannot also contain an external URL")
            if not self.asset_inline_content.strip():
                raise ValueError("An inline publication asset requires non-blank content")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class PublicationDispatchResult:
    platform: Platform
    mode: PublicationMode
    provider: str
    status: PublicationDispatchStatus
    operation_key: str
    external_url: str = ""
    external_publication_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.platform, Platform) or not isinstance(self.mode, PublicationMode):
            raise ValueError("Publication result platform and mode must use the declared V1 enums")
        if not self.provider or not self.operation_key:
            raise ValueError("Publication result provider and operation_key are required")
        if self.status is PublicationDispatchStatus.SUCCEEDED and not (
            self.external_url or self.external_publication_id
        ):
            raise ValueError("A successful publication requires an external URL or content ID")
        if self.status in {
            PublicationDispatchStatus.BLOCKED,
            PublicationDispatchStatus.UNAVAILABLE,
            PublicationDispatchStatus.FAILED,
        } and not self.reason:
            raise ValueError("A non-success publication result requires a reason")
        if self.external_url:
            parsed = urlsplit(self.external_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Publication result external_url must be credential-free HTTP(S)")


class PublicationTransport(Protocol):
    """Bounded side-effect adapter supplied by deployment code or a test.

    The core runtime contains no network implementation.  A live adapter must
    implement provider idempotency using ``request.operation_key``.
    """

    def dispatch(self, request: PublicationDispatchRequest) -> PublicationDispatchResult: ...
