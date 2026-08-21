from __future__ import annotations

import json
import re
import threading
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from integrations.ai.secrets import SecretFileReference, read_secret_file
from integrations.browser_worker import (
    BrowserJobOperation,
    BrowserWorkerClient,
    BrowserWorkerJob,
    BrowserWorkerPairing,
)
from integrations.connectors.catalog import default_connector_catalog
from integrations.connectors.types import (
    AcquisitionMode,
    AvailabilityState,
    ConnectorRequest,
    ConnectorResult,
    ConnectorRunStatus,
    Platform,
)
from integrations.errors import (
    BrowserWorkerProtocolError,
    ConnectorConfigurationError,
    IngestionValidationError,
    IntegrationError,
    NetworkAccessDisabled,
    ProviderResponseError,
    SecretLoadingError,
)
from integrations.ingestion import (
    CSVIngestionValidator,
    IngestedEvidence,
    ManualEvidenceInput,
    validate_manual_evidence,
)


@dataclass(frozen=True, slots=True)
class JSONTransportResponse:
    status_code: int
    payload: Mapping[str, Any]
    response_bytes: int
    request_id: str | None = None


class ReadOnlyJSONTransport(Protocol):
    def request_json(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        payload: Mapping[str, Any] | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> JSONTransportResponse: ...


class DisabledJSONTransport:
    """Default transport. It cannot perform any network I/O."""

    def request_json(self, **_: Any) -> JSONTransportResponse:
        raise NetworkAccessDisabled(
            "Connector networking is disabled; inject an explicitly enabled transport"
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a provider credential to a host that was not reviewed
        # in JSONAPIRouteConfig.  The caller may configure the final endpoint.
        return None


class UrllibReadOnlyJSONTransport:
    """Bounded standard-library transport requiring explicit live-network opt in.

    It performs one request only.  Provider hosts, endpoint paths and versions
    remain the responsibility of ``JSONAPIRouteConfig``; the transport merely
    enforces HTTPS again, bounds response bytes and accepts only a JSON object.
    """

    def __init__(self, *, allow_network: bool = False, opener: Callable[..., Any] | None = None):
        if not allow_network:
            raise NetworkAccessDisabled("UrllibReadOnlyJSONTransport requires allow_network=True")
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler()).open

    def request_json(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        query: Mapping[str, str],
        payload: Mapping[str, Any] | None,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> JSONTransportResponse:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ProviderResponseError("Connector transport accepts only credential-free HTTPS URLs")
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise ProviderResponseError("Connector transport accepts only GET or POST")
        if not 1 <= max_response_bytes <= 10_000_000:
            raise ProviderResponseError("Connector response byte limit is invalid")
        request_url = url
        body = None
        request_headers = dict(headers)
        request_headers.setdefault("Accept", "application/json")
        if method == "GET":
            encoded_query = urllib.parse.urlencode(dict(query))
            if encoded_query:
                separator = "&" if urlsplit(request_url).query else "?"
                request_url = f"{request_url}{separator}{encoded_query}"
        else:
            body = json.dumps(dict(payload or {}), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            url=request_url,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=timeout_seconds) as response:
                response_body = response.read(max_response_bytes + 1)
                if len(response_body) > max_response_bytes:
                    raise ProviderResponseError("Provider response exceeded the configured byte limit")
                parsed_body = json.loads(response_body.decode("utf-8"))
                if not isinstance(parsed_body, Mapping):
                    raise ProviderResponseError("Provider response root must be a JSON object")
                headers_object = getattr(response, "headers", {})
                return JSONTransportResponse(
                    status_code=int(getattr(response, "status", response.getcode())),
                    payload=dict(parsed_body),
                    response_bytes=len(response_body),
                    request_id=(headers_object.get("x-request-id") if headers_object else None),
                )
        except ProviderResponseError:
            raise
        except urllib.error.HTTPError as exc:
            raise ProviderResponseError(f"Provider returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderResponseError("Connector provider transport failed") from exc
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ProviderResponseError("Connector provider returned invalid JSON") from exc


_FIELD_NAMES = frozenset(
    {"external_id", "url", "title", "content_text", "collected_at", "attributes"}
)
_REQUEST_VALUES = frozenset(
    {"query", "window_start", "window_end", "market_code", "language_code", "max_items"}
)


@dataclass(frozen=True, slots=True)
class JSONAPIRouteConfig:
    """Explicit contract for one read-only provider endpoint.

    Endpoint paths, versions, request names and response field paths are all
    configuration.  The runtime intentionally has no guessed provider URLs.
    """

    platform: Platform
    provider: str
    base_url: str
    endpoint_path: str
    api_version: str
    method: str
    allowed_hosts: tuple[str, ...]
    secret: SecretFileReference
    auth_header: str
    auth_prefix: str
    request_field_map: Mapping[str, str]
    response_items_path: tuple[str, ...]
    response_field_map: Mapping[str, tuple[str, ...]]
    timeout_seconds: float = 10.0
    max_response_bytes: int = 2_000_000
    max_requests: int = 25

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ConnectorConfigurationError("API base_url must be credential-free HTTPS")
        if parsed.query or parsed.fragment:
            raise ConnectorConfigurationError("API base_url must not contain query or fragment")
        if parsed.hostname.lower() not in {host.lower() for host in self.allowed_hosts}:
            raise ConnectorConfigurationError("API base_url host is not explicitly allowed")
        if self.method.upper() not in {"GET", "POST"}:
            raise ConnectorConfigurationError("Read-only API routes support GET or POST only")
        if not self.provider.strip() or not self.endpoint_path.strip() or not self.api_version.strip():
            raise ConnectorConfigurationError("Provider, endpoint path and API version are required")
        if self.endpoint_path.startswith(("http://", "https://")) or ".." in self.endpoint_path.split("/"):
            raise ConnectorConfigurationError("endpoint_path must be a safe relative path")
        if any(char in self.endpoint_path for char in "?#"):
            raise ConnectorConfigurationError("endpoint_path must not contain query or fragment")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", self.api_version):
            raise ConnectorConfigurationError("api_version has an invalid format")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", self.auth_header):
            raise ConnectorConfigurationError("auth_header has an invalid format")
        if not 0.1 <= self.timeout_seconds <= 30:
            raise ConnectorConfigurationError("timeout_seconds must be between 0.1 and 30")
        if not 1 <= self.max_response_bytes <= 10_000_000:
            raise ConnectorConfigurationError("max_response_bytes is outside the safe range")
        if not 1 <= self.max_requests <= 1_000:
            raise ConnectorConfigurationError("max_requests is outside the safe range")
        if not self.response_items_path:
            raise ConnectorConfigurationError("response_items_path must be explicit")
        unknown_request = set(self.request_field_map) - _REQUEST_VALUES
        if unknown_request or not all(self.request_field_map.values()):
            raise ConnectorConfigurationError("request_field_map contains unsupported fields")
        unknown_response = set(self.response_field_map) - _FIELD_NAMES
        if unknown_response or not all(
            path and all(component for component in path)
            for path in self.response_field_map.values()
        ):
            raise ConnectorConfigurationError("response_field_map contains unsupported fields")
        if not ({"external_id", "url"} & set(self.response_field_map)):
            raise ConnectorConfigurationError("response mapping requires external_id or url")
        if not ({"title", "content_text"} & set(self.response_field_map)):
            raise ConnectorConfigurationError("response mapping requires title or content_text")
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "allowed_hosts", tuple(host.lower() for host in self.allowed_hosts))
        object.__setattr__(self, "request_field_map", MappingProxyType(dict(self.request_field_map)))
        object.__setattr__(self, "response_field_map", MappingProxyType(dict(self.response_field_map)))

    @property
    def url(self) -> str:
        parsed = urlsplit(self.base_url)
        path = "/".join(
            segment.strip("/")
            for segment in (parsed.path, self.api_version, self.endpoint_path)
            if segment.strip("/")
        )
        return urlunsplit((parsed.scheme, parsed.netloc, f"/{path}", "", ""))


class JSONAPIRoute:
    mode = AcquisitionMode.API

    def __init__(
        self,
        config: JSONAPIRouteConfig,
        *,
        transport: ReadOnlyJSONTransport | None = None,
    ):
        if config.platform is Platform.QUORA:
            raise ConnectorConfigurationError("Quora research API is explicitly unavailable in V1")
        self.config = config
        self.platform = config.platform
        self.provider = config.provider
        self.transport = transport or DisabledJSONTransport()
        self._request_count = 0
        self._lock = threading.Lock()

    def availability(self, request: ConnectorRequest) -> tuple[AvailabilityState, str]:
        if request.platform is not self.platform:
            return AvailabilityState.UNAVAILABLE, "Route platform does not match request"
        if isinstance(self.transport, DisabledJSONTransport):
            return AvailabilityState.MISSING, "Live API transport is not enabled"
        if not self.config.secret.path.is_file():
            return AvailabilityState.MISSING, "API secret file is not configured"
        return AvailabilityState.AVAILABLE, "API route is explicitly configured"

    def collect(self, request: ConnectorRequest) -> ConnectorResult:
        state, reason = self.availability(request)
        if state is not AvailabilityState.AVAILABLE:
            return _status_result(request, self.mode, self.provider, state, reason)
        try:
            secret = read_secret_file(self.config.secret)
            with self._lock:
                if self._request_count >= self.config.max_requests:
                    return _result(
                        request,
                        self.mode,
                        self.provider,
                        ConnectorRunStatus.BLOCKED,
                        "API request cap reached",
                    )
                self._request_count += 1
            request_values = {
                "query": request.query,
                "window_start": request.window_start.isoformat(),
                "window_end": request.window_end.isoformat(),
                "market_code": request.market_code,
                "language_code": request.language_code,
                "max_items": str(request.max_items),
            }
            remote_values = {
                remote_name: request_values[local_name]
                for local_name, remote_name in self.config.request_field_map.items()
            }
            response = self.transport.request_json(
                method=self.config.method,
                url=self.config.url,
                headers={self.config.auth_header: f"{self.config.auth_prefix}{secret}"},
                query=remote_values if self.config.method == "GET" else {},
                payload=remote_values if self.config.method == "POST" else None,
                timeout_seconds=self.config.timeout_seconds,
                max_response_bytes=self.config.max_response_bytes,
            )
            measured_bytes = len(
                json.dumps(response.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            if max(response.response_bytes, measured_bytes) > self.config.max_response_bytes:
                raise ProviderResponseError("Provider response exceeded the configured byte limit")
            if not 200 <= response.status_code < 300:
                raise ProviderResponseError(f"Provider returned HTTP {response.status_code}")
            items = _extract_items(response.payload, self.config.response_items_path)
            mapped = tuple(
                _map_item(item, self.config.response_field_map)
                for item in items[: request.max_items]
            )
            if not mapped:
                return _result(
                    request,
                    self.mode,
                    self.provider,
                    ConnectorRunStatus.MISSING,
                    "Provider returned no evidence items",
                )
            provenance = ({"request_id": response.request_id, "endpoint_version": self.config.api_version},)
            return ConnectorResult(
                platform=request.platform,
                status=ConnectorRunStatus.SUCCEEDED,
                operation_key=request.operation_key,
                mode=self.mode,
                provider=self.provider,
                items=mapped,
                provenance=provenance,
            )
        except (SecretLoadingError, NetworkAccessDisabled) as exc:
            return _result(request, self.mode, self.provider, ConnectorRunStatus.MISSING, str(exc))
        except (ProviderResponseError, ValueError, TypeError, KeyError, OSError, TimeoutError) as exc:
            return _result(request, self.mode, self.provider, ConnectorRunStatus.FAILED, str(exc), True)


@dataclass(frozen=True, slots=True)
class BrowserRouteConfig:
    platform: Platform
    provider: str
    allowed_hosts: tuple[str, ...]
    pairing: BrowserWorkerPairing | None = None
    ttl_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.provider or not self.allowed_hosts:
            raise ConnectorConfigurationError("Browser provider and allowed_hosts are required")
        if not 30 <= self.ttl_seconds <= 3_600:
            raise ConnectorConfigurationError("Browser job TTL must be between 30 and 3600 seconds")


class BrowserRoute:
    mode = AcquisitionMode.BROWSER

    def __init__(
        self,
        config: BrowserRouteConfig,
        *,
        client: BrowserWorkerClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.platform = config.platform
        self.provider = config.provider
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def availability(self, request: ConnectorRequest) -> tuple[AvailabilityState, str]:
        if request.platform is not self.platform:
            return AvailabilityState.UNAVAILABLE, "Route platform does not match request"
        if self.config.pairing is None or self.client is None:
            return AvailabilityState.BLOCKED, "Dedicated browser worker is not paired"
        now = self.clock()
        if not self.config.pairing.valid_at(now):
            return AvailabilityState.BLOCKED, "Dedicated browser worker pairing is expired"
        if self.platform.value not in self.config.pairing.capabilities:
            return AvailabilityState.BLOCKED, "Browser worker lacks this platform capability"
        return AvailabilityState.AVAILABLE, "Dedicated browser worker is paired"

    def collect(self, request: ConnectorRequest) -> ConnectorResult:
        state, reason = self.availability(request)
        if state is not AvailabilityState.AVAILABLE:
            return _status_result(request, self.mode, self.provider, state, reason)
        assert self.config.pairing is not None and self.client is not None
        now = self.clock()
        job = BrowserWorkerJob(
            job_id=uuid.uuid5(uuid.NAMESPACE_URL, f"growth-os:{request.operation_key}:{self.platform.value}"),
            operation_key=request.operation_key,
            platform=self.platform,
            operation=BrowserJobOperation.SEARCH,
            pairing_id=self.config.pairing.pairing_id,
            dedicated_profile_id=self.config.pairing.dedicated_profile_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self.config.ttl_seconds),
            query=request.query,
            max_items=request.max_items,
            allowed_hosts=self.config.allowed_hosts,
            payload={
                "window_start": request.window_start.isoformat(),
                "window_end": request.window_end.isoformat(),
                "market_code": request.market_code,
                "language_code": request.language_code,
            },
        )
        try:
            job.validate_pairing(self.config.pairing, now)
            self.client.submit(job)
            response = self.client.result(job.job_id)
            if response is None:
                return _result(
                    request,
                    self.mode,
                    self.provider,
                    ConnectorRunStatus.BLOCKED,
                    "Browser job was submitted and is awaiting worker completion",
                    True,
                )
            if response.operation_key != request.operation_key or response.job_fingerprint != job.fingerprint:
                raise BrowserWorkerProtocolError("Browser result does not bind the exact submitted job")
            status = response.connector_status()
            if status in {ConnectorRunStatus.SUCCEEDED, ConnectorRunStatus.PARTIAL} and not response.items:
                return _result(request, self.mode, self.provider, ConnectorRunStatus.MISSING, "Browser returned no evidence items")
            if status in {ConnectorRunStatus.SUCCEEDED, ConnectorRunStatus.PARTIAL}:
                _validate_candidate_items(response.items)
            return ConnectorResult(
                platform=request.platform,
                status=status,
                operation_key=request.operation_key,
                mode=self.mode,
                provider=self.provider,
                items=response.items,
                provenance=({"job_id": str(job.job_id), "job_fingerprint": job.fingerprint},),
                reason=response.reason,
                retryable=status in {ConnectorRunStatus.BLOCKED, ConnectorRunStatus.FAILED},
            )
        except (
            BrowserWorkerProtocolError,
            IntegrationError,
            RuntimeError,
            OSError,
            TimeoutError,
        ) as exc:
            return _result(request, self.mode, self.provider, ConnectorRunStatus.FAILED, str(exc), True)


class CSVRoute:
    mode = AcquisitionMode.CSV

    def __init__(self, platform: Platform, provider: str, *, validator: CSVIngestionValidator | None = None):
        self.platform = platform
        self.provider = provider
        self.validator = validator or CSVIngestionValidator()

    def availability(self, request: ConnectorRequest) -> tuple[AvailabilityState, str]:
        return (
            (AvailabilityState.AVAILABLE, "CSV content supplied")
            if request.metadata.get("csv_content") not in (None, "", b"")
            else (AvailabilityState.MISSING, "CSV content was not supplied")
        )

    def collect(self, request: ConnectorRequest) -> ConnectorResult:
        state, reason = self.availability(request)
        if state is not AvailabilityState.AVAILABLE:
            return _status_result(request, self.mode, self.provider, state, reason)
        try:
            values = self.validator.validate(
                request.metadata["csv_content"],
                platform=request.platform,
                source_key=str(request.metadata.get("source_key", f"{request.platform.value.lower()}-csv")),
                collection_run_key=str(request.metadata.get("collection_run_key", request.operation_key)),
                collected_by=str(request.metadata.get("collected_by", "system")),
            )
            return _evidence_result(request, self.mode, self.provider, values)
        except (IngestionValidationError, ValueError, TypeError) as exc:
            return _result(request, self.mode, self.provider, ConnectorRunStatus.FAILED, str(exc))


class ManualRoute:
    mode = AcquisitionMode.MANUAL

    def __init__(self, platform: Platform, provider: str):
        self.platform = platform
        self.provider = provider

    def availability(self, request: ConnectorRequest) -> tuple[AvailabilityState, str]:
        value = request.metadata.get("manual_evidence")
        return (
            (AvailabilityState.AVAILABLE, "Manual evidence supplied")
            if isinstance(value, ManualEvidenceInput)
            else (AvailabilityState.MISSING, "Manual evidence was not supplied")
        )

    def collect(self, request: ConnectorRequest) -> ConnectorResult:
        state, reason = self.availability(request)
        if state is not AvailabilityState.AVAILABLE:
            return _status_result(request, self.mode, self.provider, state, reason)
        value = request.metadata["manual_evidence"]
        if value.platform is not request.platform:
            return _result(request, self.mode, self.provider, ConnectorRunStatus.FAILED, "Manual evidence platform does not match request")
        try:
            return _evidence_result(request, self.mode, self.provider, (validate_manual_evidence(value),))
        except (IngestionValidationError, ValueError, TypeError) as exc:
            return _result(request, self.mode, self.provider, ConnectorRunStatus.FAILED, str(exc))


class UnavailableRoute:
    def __init__(self, platform: Platform, mode: AcquisitionMode, provider: str, reason: str):
        self.platform = platform
        self.mode = mode
        self.provider = provider
        self.reason = reason

    def availability(self, request: ConnectorRequest) -> tuple[AvailabilityState, str]:
        return AvailabilityState.UNAVAILABLE, self.reason

    def collect(self, request: ConnectorRequest) -> ConnectorResult:
        return _result(request, self.mode, self.provider, ConnectorRunStatus.UNAVAILABLE, self.reason)


class MissingRoute(UnavailableRoute):
    def availability(self, request: ConnectorRequest) -> tuple[AvailabilityState, str]:
        return AvailabilityState.MISSING, self.reason

    def collect(self, request: ConnectorRequest) -> ConnectorResult:
        return _result(request, self.mode, self.provider, ConnectorRunStatus.MISSING, self.reason)


class FallbackConnector:
    def __init__(self, platform: Platform, routes: tuple[Any, ...]):
        if not routes or any(route.platform is not platform for route in routes):
            raise ConnectorConfigurationError("Fallback routes must be non-empty and match the platform")
        self.platform = platform
        self.routes = routes

    def collect(self, request: ConnectorRequest) -> ConnectorResult:
        if request.platform is not self.platform:
            return _result(request, None, None, ConnectorRunStatus.UNAVAILABLE, "Connector platform does not match request")
        attempted: list[ConnectorResult] = []
        for route in self.routes:
            state, reason = route.availability(request)
            if state is not AvailabilityState.AVAILABLE:
                attempted.append(_status_result(request, route.mode, route.provider, state, reason))
                continue
            result = route.collect(request)
            attempted.append(result)
            if result.status in {ConnectorRunStatus.SUCCEEDED, ConnectorRunStatus.PARTIAL}:
                return result
        if not attempted:
            return _result(request, None, None, ConnectorRunStatus.UNAVAILABLE, "No routes are declared")
        rank = {
            ConnectorRunStatus.FAILED: 4,
            ConnectorRunStatus.BLOCKED: 3,
            ConnectorRunStatus.MISSING: 2,
            ConnectorRunStatus.UNAVAILABLE: 1,
        }
        final = max(attempted, key=lambda result: rank.get(result.status, 0))
        reasons = "; ".join(f"{item.provider}: {item.reason}" for item in attempted if item.reason)
        return ConnectorResult(
            platform=request.platform,
            status=final.status,
            operation_key=request.operation_key,
            mode=final.mode,
            provider=final.provider,
            reason=reasons,
            retryable=any(item.retryable for item in attempted),
        )


@dataclass(frozen=True, slots=True)
class ConnectorRuntimeConfig:
    api_routes: Mapping[Platform, JSONAPIRouteConfig] = field(default_factory=dict)
    browser_routes: Mapping[Platform, BrowserRouteConfig] = field(default_factory=dict)
    browser_clients: Mapping[Platform, BrowserWorkerClient] = field(default_factory=dict)
    api_transports: Mapping[Platform, ReadOnlyJSONTransport] = field(default_factory=dict)


def build_connector_registry(config: ConnectorRuntimeConfig | None = None) -> dict[Platform, FallbackConnector]:
    config = config or ConnectorRuntimeConfig()
    if Platform.QUORA in config.api_routes:
        raise ConnectorConfigurationError("Quora research API is explicitly unavailable in V1")
    catalog = default_connector_catalog()
    registry: dict[Platform, FallbackConnector] = {}
    for platform, descriptor in catalog.items():
        providers = {route.mode: route.provider for route in descriptor.routes}
        api_route: Any
        if platform is Platform.QUORA:
            api_route = UnavailableRoute(platform, AcquisitionMode.API, providers[AcquisitionMode.API], "Quora research API is unavailable in V1")
        elif platform in config.api_routes:
            api_config = config.api_routes[platform]
            if api_config.platform is not platform:
                raise ConnectorConfigurationError("API route registry key does not match config platform")
            if api_config.provider != providers[AcquisitionMode.API]:
                raise ConnectorConfigurationError(
                    f"{platform.value} API provider must be {providers[AcquisitionMode.API]}"
                )
            api_route = JSONAPIRoute(api_config, transport=config.api_transports.get(platform))
        else:
            api_route = MissingRoute(
                platform,
                AcquisitionMode.API,
                providers[AcquisitionMode.API],
                "API route is not configured",
            )

        browser_config = config.browser_routes.get(platform) or BrowserRouteConfig(
            platform=platform,
            provider=providers[AcquisitionMode.BROWSER],
            allowed_hosts=_DEFAULT_BROWSER_HOSTS[platform],
        )
        routes_by_mode = {
            AcquisitionMode.API: api_route,
            AcquisitionMode.BROWSER: BrowserRoute(browser_config, client=config.browser_clients.get(platform)),
            AcquisitionMode.CSV: CSVRoute(platform, providers[AcquisitionMode.CSV]),
            AcquisitionMode.MANUAL: ManualRoute(platform, providers[AcquisitionMode.MANUAL]),
        }
        ordered = tuple(routes_by_mode[item.mode] for item in descriptor.routes)
        registry[platform] = FallbackConnector(platform, ordered)
    return registry


_DEFAULT_BROWSER_HOSTS: dict[Platform, tuple[str, ...]] = {
    Platform.PINTEREST: ("pinterest.com",),
    Platform.QUORA: ("quora.com",),
    Platform.TIKTOK: ("tiktok.com", "ads.tiktok.com"),
    Platform.SHOPIFY: ("shopify.com",),
    Platform.GOOGLE_SEARCH: ("google.com",),
    Platform.GOOGLE_SEARCH_CONSOLE: ("search.google.com",),
    Platform.GOOGLE_ANALYTICS_4: ("analytics.google.com",),
}


def _extract_items(payload: Mapping[str, Any], path: tuple[str, ...]) -> list[Mapping[str, Any]]:
    value: Any = payload
    for component in path:
        if not isinstance(value, Mapping) or component not in value:
            raise ProviderResponseError("Provider response does not match response_items_path")
        value = value[component]
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ProviderResponseError("Provider response items must be a JSON array of objects")
    return value


def _path_value(item: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = item
    for component in path:
        if not isinstance(value, Mapping) or component not in value:
            return None
        value = value[component]
    return value


def _map_item(item: Mapping[str, Any], mapping: Mapping[str, tuple[str, ...]]) -> Mapping[str, Any]:
    mapped = {name: _path_value(item, path) for name, path in mapping.items()}
    if not mapped.get("external_id") and not mapped.get("url"):
        raise ProviderResponseError("Mapped evidence requires external_id or url")
    if not mapped.get("title") and not mapped.get("content_text"):
        raise ProviderResponseError("Mapped evidence requires title or content_text")
    attributes = mapped.get("attributes")
    if attributes is not None and not isinstance(attributes, Mapping):
        raise ProviderResponseError("Mapped attributes must be an object")
    mapped["attributes"] = dict(attributes or {})
    return mapped


def _validate_candidate_items(items: tuple[Mapping[str, Any], ...]) -> None:
    for item in items:
        if not item.get("external_id") and not item.get("url"):
            raise ProviderResponseError("Evidence item requires external_id or url")
        if not item.get("title") and not item.get("content_text"):
            raise ProviderResponseError("Evidence item requires title or content_text")


def _evidence_result(
    request: ConnectorRequest,
    mode: AcquisitionMode,
    provider: str,
    evidence: tuple[IngestedEvidence, ...],
) -> ConnectorResult:
    items = tuple(
        {
            "external_id": item.external_id,
            "url": item.url,
            "title": item.title,
            "content_text": item.content_text,
            "collected_at": item.provenance.collected_at.isoformat(),
            "attributes": dict(item.attributes),
            "dedupe_key": item.dedupe_key,
        }
        for item in evidence
    )
    provenance = tuple(
        {
            "source_key": item.provenance.source_key,
            "collection_run_key": item.provenance.collection_run_key,
            "payload_digest": item.provenance.payload_digest,
        }
        for item in evidence
    )
    return ConnectorResult(
        platform=request.platform,
        status=ConnectorRunStatus.SUCCEEDED,
        operation_key=request.operation_key,
        mode=mode,
        provider=provider,
        items=items,
        provenance=provenance,
    )


def _status_result(
    request: ConnectorRequest,
    mode: AcquisitionMode,
    provider: str,
    state: AvailabilityState,
    reason: str,
) -> ConnectorResult:
    status = {
        AvailabilityState.MISSING: ConnectorRunStatus.MISSING,
        AvailabilityState.BLOCKED: ConnectorRunStatus.BLOCKED,
        AvailabilityState.UNAVAILABLE: ConnectorRunStatus.UNAVAILABLE,
        AvailabilityState.AVAILABLE: ConnectorRunStatus.FAILED,
    }[state]
    return _result(request, mode, provider, status, reason, status is ConnectorRunStatus.BLOCKED)


def _result(
    request: ConnectorRequest,
    mode: AcquisitionMode | None,
    provider: str | None,
    status: ConnectorRunStatus,
    reason: str,
    retryable: bool = False,
) -> ConnectorResult:
    return ConnectorResult(
        platform=request.platform,
        status=status,
        operation_key=request.operation_key,
        mode=mode,
        provider=provider,
        reason=reason,
        retryable=retryable,
    )
