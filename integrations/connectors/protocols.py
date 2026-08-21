from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol

from integrations.connectors.types import AcquisitionMode, ConnectorRequest, ConnectorResult, Platform


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    platform: Platform
    source_key: str
    collected_at: datetime
    external_id: str | None
    url: str | None
    title: str
    content_text: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


class Connector(Protocol):
    platform: Platform

    def collect(self, request: ConnectorRequest) -> ConnectorResult: ...


class ConnectorRoute(Protocol):
    platform: Platform
    mode: AcquisitionMode
    provider: str

    def collect(self, request: ConnectorRequest) -> ConnectorResult: ...
