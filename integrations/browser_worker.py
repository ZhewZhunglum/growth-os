from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from integrations.ai.secrets import SecretFileReference, read_secret_file
from integrations.connectors.types import ConnectorRunStatus, Platform
from integrations.errors import BrowserWorkerProtocolError, NetworkAccessDisabled


class BrowserJobOperation(StrEnum):
    SEARCH = "SEARCH"
    COLLECT = "COLLECT"
    EXPORT = "EXPORT"


class BrowserWorkerJobStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class BrowserWorkerPairing:
    pairing_id: uuid.UUID
    worker_id: str
    dedicated_profile_id: str
    dedicated_profile_label: str
    browser_family: str
    paired_at: datetime
    expires_at: datetime
    capabilities: tuple[str, ...]
    dedicated_profile: bool = True

    def __post_init__(self) -> None:
        if not self.dedicated_profile:
            raise BrowserWorkerProtocolError("Browser worker must use a dedicated profile")
        if not self.worker_id or not self.dedicated_profile_id or not self.dedicated_profile_label:
            raise BrowserWorkerProtocolError("Pairing worker and dedicated profile metadata are required")
        if self.paired_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise BrowserWorkerProtocolError("Pairing timestamps must be timezone-aware")
        if self.expires_at <= self.paired_at:
            raise BrowserWorkerProtocolError("Pairing expiry must be after paired_at")
        if not self.capabilities:
            raise BrowserWorkerProtocolError("Pairing must declare at least one capability")

    def valid_at(self, when: datetime) -> bool:
        return self.paired_at <= when < self.expires_at


@dataclass(frozen=True, slots=True)
class BrowserWorkerJob:
    job_id: uuid.UUID
    operation_key: str
    platform: Platform
    operation: BrowserJobOperation
    pairing_id: uuid.UUID
    dedicated_profile_id: str
    created_at: datetime
    expires_at: datetime
    query: str
    max_items: int
    allowed_hosts: tuple[str, ...]
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", self.operation_key):
            raise BrowserWorkerProtocolError("Browser job operation_key is invalid")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise BrowserWorkerProtocolError("Browser job timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise BrowserWorkerProtocolError("Browser job must expire after creation")
        if not self.dedicated_profile_id:
            raise BrowserWorkerProtocolError("Browser job must bind an exact dedicated profile")
        if not self.query.strip() or not 1 <= self.max_items <= 1_000:
            raise BrowserWorkerProtocolError("Browser job query or max_items is invalid")
        if not self.allowed_hosts or any(not _valid_host(host) for host in self.allowed_hosts):
            raise BrowserWorkerProtocolError("Browser job requires valid explicit allowed_hosts")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def fingerprint(self) -> str:
        canonical = {
            "job_id": str(self.job_id),
            "operation_key": self.operation_key,
            "platform": self.platform.value,
            "operation": self.operation.value,
            "pairing_id": str(self.pairing_id),
            "dedicated_profile_id": self.dedicated_profile_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "query": self.query,
            "max_items": self.max_items,
            "allowed_hosts": self.allowed_hosts,
            "payload": dict(self.payload),
        }
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def validate_pairing(self, pairing: BrowserWorkerPairing, when: datetime) -> None:
        if pairing.pairing_id != self.pairing_id:
            raise BrowserWorkerProtocolError("Browser job pairing_id does not match worker pairing")
        if pairing.dedicated_profile_id != self.dedicated_profile_id:
            raise BrowserWorkerProtocolError("Browser job profile does not match paired profile")
        if not pairing.valid_at(when) or not (self.created_at <= when < self.expires_at):
            raise BrowserWorkerProtocolError("Browser job or worker pairing is expired")
        if self.platform.value not in pairing.capabilities:
            raise BrowserWorkerProtocolError("Paired browser worker lacks the platform capability")


@dataclass(frozen=True, slots=True)
class BrowserWorkerResult:
    job_id: uuid.UUID
    operation_key: str
    job_fingerprint: str
    status: BrowserWorkerJobStatus
    completed_at: datetime
    items: tuple[Mapping[str, Any], ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.completed_at.tzinfo is None:
            raise BrowserWorkerProtocolError("Browser result completed_at must be timezone-aware")
        if self.status in {
            BrowserWorkerJobStatus.BLOCKED,
            BrowserWorkerJobStatus.FAILED,
            BrowserWorkerJobStatus.CANCELLED,
        } and not self.reason:
            raise BrowserWorkerProtocolError("Non-success browser result requires a reason")
        object.__setattr__(self, "items", tuple(MappingProxyType(dict(item)) for item in self.items))

    def connector_status(self) -> ConnectorRunStatus:
        return {
            BrowserWorkerJobStatus.SUCCEEDED: ConnectorRunStatus.SUCCEEDED,
            BrowserWorkerJobStatus.PARTIAL: ConnectorRunStatus.PARTIAL,
            BrowserWorkerJobStatus.BLOCKED: ConnectorRunStatus.BLOCKED,
            BrowserWorkerJobStatus.FAILED: ConnectorRunStatus.FAILED,
            BrowserWorkerJobStatus.CANCELLED: ConnectorRunStatus.BLOCKED,
        }[self.status]


class BrowserWorkerClient(Protocol):
    """Envelope transport only; V1 execution is implemented by a separate worker."""

    def submit(self, job: BrowserWorkerJob) -> None: ...

    def result(self, job_id: uuid.UUID) -> BrowserWorkerResult | None: ...

    def cancel(self, job_id: uuid.UUID, reason: str) -> None: ...


@dataclass(frozen=True, slots=True)
class HTTPBrowserWorkerConfig:
    """Explicit contract for one already-paired, separately operated worker."""

    base_url: str
    secret: SecretFileReference
    allowed_worker_hosts: tuple[str, ...]
    timeout_seconds: float = 10.0
    max_request_bytes: int = 1_000_000
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        hosts = tuple(host.strip().lower() for host in self.allowed_worker_hosts)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise BrowserWorkerProtocolError("Browser worker base_url must be a credential-free HTTPS URL")
        if not hosts or any(not _valid_host(host) for host in hosts):
            raise BrowserWorkerProtocolError("Browser worker requires a valid explicit host allowlist")
        if parsed.hostname.lower() not in hosts:
            raise BrowserWorkerProtocolError("Browser worker base_url host is not allowlisted")
        decoded_path = urllib.parse.unquote(parsed.path)
        if any(segment in {".", ".."} for segment in decoded_path.split("/")):
            raise BrowserWorkerProtocolError("Browser worker base_url path is invalid")
        if not 0.1 <= self.timeout_seconds <= 30:
            raise BrowserWorkerProtocolError("Browser worker timeout must be between 0.1 and 30 seconds")
        if not 1 <= self.max_request_bytes <= 10_000_000:
            raise BrowserWorkerProtocolError("Browser worker request byte limit is invalid")
        if not 1 <= self.max_response_bytes <= 10_000_000:
            raise BrowserWorkerProtocolError("Browser worker response byte limit is invalid")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "allowed_worker_hosts", hosts)


class _BrowserWorkerNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class _JobBinding:
    job_id: uuid.UUID
    operation_key: str
    fingerprint: str


class HTTPBrowserWorkerClient:
    """Bounded HTTP envelope client; disabled unless explicitly opted in.

    It does not control a browser itself.  It only submits immutable jobs to a
    separately paired worker and rejects any response that is not bound to the
    exact job id, operation key and fingerprint.
    """

    def __init__(
        self,
        config: HTTPBrowserWorkerConfig,
        *,
        allow_network: bool = False,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not allow_network:
            raise NetworkAccessDisabled("HTTPBrowserWorkerClient requires allow_network=True")
        self.config = config
        self._opener = opener or urllib.request.build_opener(_BrowserWorkerNoRedirectHandler()).open
        self._bindings: dict[uuid.UUID, _JobBinding] = {}
        self._lock = threading.Lock()

    def submit(self, job: BrowserWorkerJob) -> None:
        binding = _JobBinding(job.job_id, job.operation_key, job.fingerprint)
        with self._lock:
            existing = self._bindings.get(job.job_id)
            if existing is not None and existing != binding:
                raise BrowserWorkerProtocolError("Browser job id is already bound to a different operation")
        response = self._request_json(
            method="POST",
            suffix="/jobs",
            payload={
                "job_id": str(job.job_id),
                "operation_key": job.operation_key,
                "job_fingerprint": job.fingerprint,
                "platform": job.platform.value,
                "operation": job.operation.value,
                "pairing_id": str(job.pairing_id),
                "dedicated_profile_id": job.dedicated_profile_id,
                "created_at": job.created_at.isoformat(),
                "expires_at": job.expires_at.isoformat(),
                "query": job.query,
                "max_items": job.max_items,
                "allowed_hosts": list(job.allowed_hosts),
                "payload": dict(job.payload),
            },
        )
        self._validate_binding(response, binding)
        if response.get("accepted") is not True:
            raise BrowserWorkerProtocolError("Browser worker did not explicitly accept the exact job")
        with self._lock:
            prior = self._bindings.setdefault(job.job_id, binding)
            if prior != binding:
                raise BrowserWorkerProtocolError("Browser job binding changed during submit")

    def result(self, job_id: uuid.UUID) -> BrowserWorkerResult | None:
        binding = self._known_binding(job_id)
        query = urllib.parse.urlencode(
            {"operation_key": binding.operation_key, "job_fingerprint": binding.fingerprint}
        )
        response = self._request_json(
            method="GET",
            suffix=f"/jobs/{job_id}?{query}",
            payload=None,
        )
        self._validate_binding(response, binding)
        if response.get("state") == "PENDING":
            return None
        try:
            status = BrowserWorkerJobStatus(response["status"])
            completed_at = _parse_aware_datetime(response["completed_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BrowserWorkerProtocolError("Browser worker result is missing a valid status or completed_at") from exc
        items = response.get("items", ())
        if not isinstance(items, (list, tuple)) or any(not isinstance(item, Mapping) for item in items):
            raise BrowserWorkerProtocolError("Browser worker result items must be JSON objects")
        reason = response.get("reason", "")
        if not isinstance(reason, str):
            raise BrowserWorkerProtocolError("Browser worker result reason must be text")
        return BrowserWorkerResult(
            job_id=binding.job_id,
            operation_key=binding.operation_key,
            job_fingerprint=binding.fingerprint,
            status=status,
            completed_at=completed_at,
            items=tuple(dict(item) for item in items),
            reason=reason,
        )

    def cancel(self, job_id: uuid.UUID, reason: str) -> None:
        binding = self._known_binding(job_id)
        reason = (reason or "").strip()
        if not reason or len(reason) > 2_000:
            raise BrowserWorkerProtocolError("Browser job cancellation requires a bounded reason")
        response = self._request_json(
            method="POST",
            suffix=f"/jobs/{job_id}/cancel",
            payload={
                "job_id": str(binding.job_id),
                "operation_key": binding.operation_key,
                "job_fingerprint": binding.fingerprint,
                "reason": reason,
            },
        )
        self._validate_binding(response, binding)
        if response.get("cancelled") is not True:
            raise BrowserWorkerProtocolError("Browser worker did not confirm cancellation of the exact job")

    def _known_binding(self, job_id: uuid.UUID) -> _JobBinding:
        with self._lock:
            binding = self._bindings.get(job_id)
        if binding is None:
            raise BrowserWorkerProtocolError("Browser result/cancel requires an exact previously submitted job")
        return binding

    def _request_json(self, *, method: str, suffix: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        url = f"{self.config.base_url}{suffix}"
        self._validate_transport_url(url)
        body = None
        if payload is not None:
            body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(body) > self.config.max_request_bytes:
                raise BrowserWorkerProtocolError("Browser worker request exceeded the configured byte limit")
        token = read_secret_file(self.config.secret)
        request = urllib.request.Request(
            url=url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
            method=method,
        )
        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                self._validate_transport_url(final_url)
                if final_url != url:
                    raise BrowserWorkerProtocolError("Browser worker redirects are forbidden")
                status_value = getattr(response, "status", None)
                if status_value is None:
                    status_value = response.getcode()
                status = int(status_value)
                if not 200 <= status < 300:
                    raise BrowserWorkerProtocolError(f"Browser worker returned HTTP {status}")
                raw = response.read(self.config.max_response_bytes + 1)
                if len(raw) > self.config.max_response_bytes:
                    raise BrowserWorkerProtocolError("Browser worker response exceeded the configured byte limit")
        except BrowserWorkerProtocolError:
            raise
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise BrowserWorkerProtocolError("Browser worker redirects are forbidden") from exc
            raise BrowserWorkerProtocolError(f"Browser worker returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BrowserWorkerProtocolError("Browser worker transport failed") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise BrowserWorkerProtocolError("Browser worker returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise BrowserWorkerProtocolError("Browser worker response root must be a JSON object")
        return decoded

    def _validate_transport_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.hostname.lower() not in self.config.allowed_worker_hosts
        ):
            raise BrowserWorkerProtocolError("Browser worker request URL violates HTTPS/host restrictions")

    @staticmethod
    def _validate_binding(payload: Mapping[str, Any], binding: _JobBinding) -> None:
        if (
            payload.get("job_id") != str(binding.job_id)
            or payload.get("operation_key") != binding.operation_key
            or payload.get("job_fingerprint") != binding.fingerprint
        ):
            raise BrowserWorkerProtocolError("Browser worker response does not bind the exact submitted job")


def _parse_aware_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("datetime must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return parsed


def _valid_host(host: str) -> bool:
    return bool(re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", host))
