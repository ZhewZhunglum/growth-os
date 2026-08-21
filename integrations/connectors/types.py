from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class Platform(StrEnum):
    PINTEREST = "PINTEREST"
    QUORA = "QUORA"
    TIKTOK = "TIKTOK"
    SHOPIFY = "SHOPIFY"
    GOOGLE_SEARCH = "GOOGLE_SEARCH"
    GOOGLE_SEARCH_CONSOLE = "GOOGLE_SEARCH_CONSOLE"
    GOOGLE_ANALYTICS_4 = "GOOGLE_ANALYTICS_4"


class AcquisitionMode(StrEnum):
    API = "API"
    BROWSER = "BROWSER"
    CSV = "CSV"
    MANUAL = "MANUAL"


class AvailabilityState(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class ConnectorRunStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RouteAvailability:
    mode: AcquisitionMode
    state: AvailabilityState
    provider: str
    priority: int
    reason: str

    def __post_init__(self) -> None:
        if not self.provider or not self.reason:
            raise ValueError("Connector route provider and reason are required")
        if self.priority < 1:
            raise ValueError("Connector route priority must be positive")


@dataclass(frozen=True, slots=True)
class ConnectorDescriptor:
    platform: Platform
    routes: tuple[RouteAvailability, ...]

    def __post_init__(self) -> None:
        modes = [route.mode for route in self.routes]
        if set(modes) != set(AcquisitionMode) or len(modes) != len(AcquisitionMode):
            raise ValueError("Every connector must declare API, BROWSER, CSV, and MANUAL exactly once")
        priorities = [route.priority for route in self.routes]
        if len(priorities) != len(set(priorities)):
            raise ValueError("Connector route priorities must be unique")
        object.__setattr__(self, "routes", tuple(sorted(self.routes, key=lambda item: item.priority)))

    @property
    def next_available_route(self) -> RouteAvailability | None:
        return next((route for route in self.routes if route.state is AvailabilityState.AVAILABLE), None)


_OPERATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


@dataclass(frozen=True, slots=True)
class ConnectorRequest:
    platform: Platform
    operation_key: str
    query: str
    window_start: datetime
    window_end: datetime
    market_code: str
    language_code: str
    max_items: int = 50
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _OPERATION_KEY.fullmatch(self.operation_key):
            raise ValueError("operation_key has an invalid format")
        if not self.query.strip() or len(self.query) > 2_000:
            raise ValueError("query must contain 1-2000 characters")
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            raise ValueError("Connector windows must use timezone-aware datetimes")
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be later than window_start")
        if not 1 <= self.max_items <= 1_000:
            raise ValueError("max_items must be between 1 and 1000")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    platform: Platform
    status: ConnectorRunStatus
    operation_key: str
    mode: AcquisitionMode | None
    provider: str | None
    items: tuple[Mapping[str, Any], ...] = ()
    provenance: tuple[Mapping[str, Any], ...] = ()
    reason: str = ""
    retryable: bool = False

    def __post_init__(self) -> None:
        if not self.operation_key:
            raise ValueError("Connector result operation_key is required")
        if self.status in {
            ConnectorRunStatus.MISSING,
            ConnectorRunStatus.BLOCKED,
            ConnectorRunStatus.UNAVAILABLE,
            ConnectorRunStatus.FAILED,
        } and not self.reason:
            raise ValueError("Non-success connector results require a reason")
        object.__setattr__(self, "items", tuple(MappingProxyType(dict(item)) for item in self.items))
        object.__setattr__(
            self, "provenance", tuple(MappingProxyType(dict(item)) for item in self.provenance)
        )
