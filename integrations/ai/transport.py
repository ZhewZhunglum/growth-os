from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from integrations.errors import NetworkAccessDisabled, ProviderResponseError


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    payload: Mapping[str, Any]
    request_id: str | None = None


class HTTPTransport(Protocol):
    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse: ...


class DisabledHTTPTransport:
    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        raise NetworkAccessDisabled(
            "Live AI networking is disabled; inject an explicitly enabled transport to opt in"
        )


class UrllibHTTPTransport:
    """Bounded one-shot AI transport requiring explicit live-network opt in."""

    def __init__(
        self,
        *,
        allow_network: bool = False,
        allowed_hosts: tuple[str, ...] | None = None,
        max_request_bytes: int = 2_000_000,
        max_response_bytes: int = 10_000_000,
        opener: Callable[..., Any] | None = None,
    ):
        if not allow_network:
            raise NetworkAccessDisabled("UrllibHTTPTransport requires allow_network=True")
        normalized_hosts = tuple(host.strip().lower() for host in (allowed_hosts or ("api.deepseek.com",)))
        if not normalized_hosts or any(not _valid_host(host) for host in normalized_hosts):
            raise ProviderResponseError("AI transport requires a valid explicit host allowlist")
        if not 1 <= max_request_bytes <= 20_000_000:
            raise ProviderResponseError("AI request byte limit is invalid")
        if not 1 <= max_response_bytes <= 20_000_000:
            raise ProviderResponseError("AI response byte limit is invalid")
        self.allowed_hosts = frozenset(normalized_hosts)
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler()).open

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        _validate_url(url, self.allowed_hosts)
        if not 0.1 <= timeout_seconds <= 120:
            raise ProviderResponseError("AI provider timeout must be between 0.1 and 120 seconds")
        request_headers = dict(headers)
        if any(name.lower() in {"host", "content-length"} for name in request_headers):
            raise ProviderResponseError("AI transport does not accept caller-supplied Host or Content-Length")
        request_headers.setdefault("Accept", "application/json")
        request_headers.setdefault("Content-Type", "application/json")
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProviderResponseError("AI provider request is not JSON serializable") from exc
        if len(body) > self.max_request_bytes:
            raise ProviderResponseError("AI provider request exceeded the configured byte limit")
        request = urllib.request.Request(url=url, data=body, headers=request_headers, method="POST")
        try:
            # Exactly one opener call: no automatic retry and no redirect.
            with self._opener(request, timeout=timeout_seconds) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                _validate_url(final_url, self.allowed_hosts)
                if final_url != url:
                    raise ProviderResponseError("AI provider redirects are forbidden")
                status_value = getattr(response, "status", None)
                if status_value is None:
                    status_value = response.getcode()
                status = int(status_value)
                if not 200 <= status < 300:
                    raise ProviderResponseError(f"AI provider returned HTTP {status}")
                response_body = response.read(self.max_response_bytes + 1)
                if len(response_body) > self.max_response_bytes:
                    raise ProviderResponseError("AI provider response exceeded the configured byte limit")
                parsed = json.loads(response_body.decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise ProviderResponseError("AI provider response root must be an object")
                response_headers = getattr(response, "headers", {})
                return TransportResponse(
                    status_code=status,
                    payload=parsed,
                    request_id=response_headers.get("x-request-id") if response_headers else None,
                )
        except ProviderResponseError:
            raise
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ProviderResponseError("AI provider redirects are forbidden") from exc
            raise ProviderResponseError(f"AI provider returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderResponseError("AI provider transport failed") from exc
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ProviderResponseError("AI provider returned invalid JSON") from exc


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _valid_host(host: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
            host,
        )
    )


def _validate_url(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.hostname.lower() not in allowed_hosts
    ):
        raise ProviderResponseError("AI transport accepts only allowlisted credential-free HTTPS URLs")
