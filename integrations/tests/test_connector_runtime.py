from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from integrations.ai.secrets import SecretFileReference
from integrations.browser_worker import (
    BrowserWorkerJob,
    BrowserWorkerJobStatus,
    BrowserWorkerPairing,
    BrowserWorkerResult,
)
from integrations.connectors.runtime import (
    BrowserRouteConfig,
    ConnectorRuntimeConfig,
    DisabledJSONTransport,
    JSONAPIRouteConfig,
    JSONTransportResponse,
    UrllibReadOnlyJSONTransport,
    build_connector_registry,
)
from integrations.connectors.catalog import default_connector_catalog
from integrations.connectors.types import (
    AcquisitionMode,
    ConnectorRequest,
    ConnectorRunStatus,
    Platform,
)
from integrations.errors import ConnectorConfigurationError, NetworkAccessDisabled, ProviderResponseError
from integrations.ingestion import ManualEvidenceInput


NOW = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)


def request(platform: Platform, **metadata: Any) -> ConnectorRequest:
    return ConnectorRequest(
        platform=platform,
        operation_key=f"daily:2026-08-21:{platform.value.lower()}",
        query="focus supplements",
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        market_code="US",
        language_code="en",
        max_items=20,
        metadata=metadata,
    )


class FakeJSONTransport:
    def __init__(self, payload: Mapping[str, Any], *, response_bytes: int = 512):
        self.payload = payload
        self.response_bytes = response_bytes
        self.calls: list[dict[str, Any]] = []

    def request_json(self, **kwargs: Any) -> JSONTransportResponse:
        self.calls.append(kwargs)
        return JSONTransportResponse(
            status_code=200,
            payload=self.payload,
            response_bytes=self.response_bytes,
            request_id="request-1",
        )


class FakeURLResponse:
    def __init__(self, payload: Any, *, status: int = 200, raw: bytes | None = None):
        self.body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = {"x-request-id": "fake-urlopen-1"}

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeURLOpener:
    def __init__(self, response: FakeURLResponse):
        self.response = response
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        return self.response


class FakeBrowserClient:
    def __init__(self, *, succeed: bool):
        self.succeed = succeed
        self.submitted: list[BrowserWorkerJob] = []

    def submit(self, job: BrowserWorkerJob) -> None:
        self.submitted.append(job)

    def result(self, job_id: uuid.UUID) -> BrowserWorkerResult | None:
        if not self.succeed:
            return None
        job = self.submitted[-1]
        return BrowserWorkerResult(
            job_id=job_id,
            operation_key=job.operation_key,
            job_fingerprint=job.fingerprint,
            status=BrowserWorkerJobStatus.SUCCEEDED,
            completed_at=NOW,
            items=({"external_id": "browser-1", "title": "Browser result"},),
        )

    def cancel(self, job_id: uuid.UUID, reason: str) -> None:
        return None


def api_config(platform: Platform, secret_path: Path, *, provider: str | None = None) -> JSONAPIRouteConfig:
    default_provider = next(
        route.provider
        for route in default_connector_catalog()[platform].routes
        if route.mode is AcquisitionMode.API
    )
    return JSONAPIRouteConfig(
        platform=platform,
        provider=provider or default_provider,
        base_url="https://provider.example.test",
        endpoint_path="configured/search",
        api_version="v-configured",
        method="POST",
        allowed_hosts=("provider.example.test",),
        secret=SecretFileReference(secret_path),
        auth_header="Authorization",
        auth_prefix="Bearer ",
        request_field_map={
            "query": "keyword",
            "window_start": "start_time",
            "window_end": "end_time",
            "max_items": "limit",
        },
        response_items_path=("data", "items"),
        response_field_map={
            "external_id": ("id",),
            "url": ("permalink",),
            "title": ("headline",),
            "content_text": ("text",),
        },
        max_requests=1,
    )


class ConnectorRuntimeTests(unittest.TestCase):
    def test_default_registry_covers_seven_platforms_without_network(self):
        registry = build_connector_registry()
        self.assertEqual(set(registry), set(Platform))
        for platform, connector in registry.items():
            result = connector.collect(request(platform))
            self.assertIn(
                result.status,
                {ConnectorRunStatus.MISSING, ConnectorRunStatus.BLOCKED, ConnectorRunStatus.UNAVAILABLE},
            )
            self.assertNotEqual(result.status, ConnectorRunStatus.SUCCEEDED)

    def test_disabled_transport_never_performs_network(self):
        with self.assertRaises(NetworkAccessDisabled):
            DisabledJSONTransport().request_json()

    def test_urllib_transport_requires_explicit_network_opt_in(self):
        with self.assertRaises(NetworkAccessDisabled):
            UrllibReadOnlyJSONTransport()

    def test_urllib_transport_uses_injected_opener_and_validates_bounded_json(self):
        opener = FakeURLOpener(FakeURLResponse({"data": {"items": []}}))
        transport = UrllibReadOnlyJSONTransport(allow_network=True, opener=opener)
        response = transport.request_json(
            method="GET",
            url="https://provider.example.test/v1/search",
            headers={"Authorization": "Bearer redacted-test"},
            query={"q": "focus"},
            payload=None,
            timeout_seconds=3,
            max_response_bytes=1_024,
        )

        self.assertEqual(response.payload, {"data": {"items": []}})
        self.assertEqual(response.request_id, "fake-urlopen-1")
        self.assertEqual(len(opener.calls), 1)
        sent_request, timeout = opener.calls[0]
        self.assertEqual(timeout, 3)
        self.assertEqual(sent_request.full_url, "https://provider.example.test/v1/search?q=focus")

    def test_urllib_transport_rejects_oversized_or_non_object_json_without_retry(self):
        oversized_opener = FakeURLOpener(FakeURLResponse({}, raw=b"{" + b"x" * 100 + b"}"))
        transport = UrllibReadOnlyJSONTransport(allow_network=True, opener=oversized_opener)
        with self.assertRaisesRegex(ProviderResponseError, "byte limit"):
            transport.request_json(
                method="GET",
                url="https://provider.example.test/v1/search",
                headers={},
                query={},
                payload=None,
                timeout_seconds=3,
                max_response_bytes=20,
            )
        self.assertEqual(len(oversized_opener.calls), 1)

        list_opener = FakeURLOpener(FakeURLResponse([{"item": 1}]))
        transport = UrllibReadOnlyJSONTransport(allow_network=True, opener=list_opener)
        with self.assertRaisesRegex(ProviderResponseError, "JSON object"):
            transport.request_json(
                method="POST",
                url="https://provider.example.test/v1/search",
                headers={},
                query={},
                payload={"q": "focus"},
                timeout_seconds=3,
                max_response_bytes=1_024,
            )
        self.assertEqual(len(list_opener.calls), 1)

    def test_tiktok_uses_configured_tikhub_api_before_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "tikhub.secret"
            secret.write_text("test-only-secret", encoding="utf-8")
            transport = FakeJSONTransport(
                {"data": {"items": [{"id": "tt-1", "permalink": "https://tiktok.com/video/1", "headline": "Hook", "text": "Body"}]}}
            )
            config = api_config(Platform.TIKTOK, secret, provider="tikhub-api")
            registry = build_connector_registry(
                ConnectorRuntimeConfig(
                    api_routes={Platform.TIKTOK: config},
                    api_transports={Platform.TIKTOK: transport},
                )
            )
            result = registry[Platform.TIKTOK].collect(request(Platform.TIKTOK))
            self.assertEqual(result.status, ConnectorRunStatus.SUCCEEDED)
            self.assertEqual(result.mode, AcquisitionMode.API)
            self.assertEqual(result.provider, "tikhub-api")
            self.assertEqual(transport.calls[0]["url"], "https://provider.example.test/v-configured/configured/search")
            self.assertEqual(transport.calls[0]["payload"]["keyword"], "focus supplements")
            self.assertNotIn("test-only-secret", str(transport.calls[0]["payload"]))

    def test_api_request_cap_fails_closed_and_falls_back_to_manual(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "pinterest.secret"
            secret.write_text("test-only-secret", encoding="utf-8")
            transport = FakeJSONTransport(
                {"data": {"items": [{"id": "pin-1", "permalink": "https://pinterest.com/pin/1", "headline": "Pin", "text": "Text"}]}}
            )
            registry = build_connector_registry(
                ConnectorRuntimeConfig(
                    api_routes={Platform.PINTEREST: api_config(Platform.PINTEREST, secret)},
                    api_transports={Platform.PINTEREST: transport},
                )
            )
            connector = registry[Platform.PINTEREST]
            self.assertEqual(connector.collect(request(Platform.PINTEREST)).mode, AcquisitionMode.API)
            manual = ManualEvidenceInput(
                platform=Platform.PINTEREST,
                source_key="pinterest-manual",
                collection_run_key="daily-2026-08-21",
                collected_by="operator-1",
                collected_at=NOW,
                url="https://pinterest.com/pin/2",
                title="Manual fallback",
            )
            result = connector.collect(request(Platform.PINTEREST, manual_evidence=manual))
            self.assertEqual(result.status, ConnectorRunStatus.SUCCEEDED)
            self.assertEqual(result.mode, AcquisitionMode.MANUAL)
            self.assertEqual(len(transport.calls), 1)

    def test_csv_is_validated_and_used_when_api_and_browser_are_missing(self):
        csv_content = (
            "external_id,url,title,collected_at\n"
            "g-1,https://example.com/result,Search result,2026-08-21T08:00:00+00:00\n"
        )
        connector = build_connector_registry()[Platform.GOOGLE_SEARCH]
        result = connector.collect(
            request(
                Platform.GOOGLE_SEARCH,
                csv_content=csv_content,
                source_key="google-csv",
                collection_run_key="daily-2026-08-21",
                collected_by="operator-1",
            )
        )
        self.assertEqual(result.status, ConnectorRunStatus.SUCCEEDED)
        self.assertEqual(result.mode, AcquisitionMode.CSV)
        self.assertEqual(result.items[0]["external_id"], "g-1")

    def test_quora_browser_is_first_and_api_is_never_attempted(self):
        pairing = BrowserWorkerPairing(
            pairing_id=uuid.uuid4(),
            worker_id="worker-1",
            dedicated_profile_id="quora-profile",
            dedicated_profile_label="Growth OS Quora",
            browser_family="chromium",
            paired_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(days=1),
            capabilities=(Platform.QUORA.value,),
        )
        client = FakeBrowserClient(succeed=True)
        config = ConnectorRuntimeConfig(
            browser_routes={
                Platform.QUORA: BrowserRouteConfig(
                    platform=Platform.QUORA,
                    provider="quora-browser-worker",
                    allowed_hosts=("quora.com",),
                    pairing=pairing,
                )
            },
            browser_clients={Platform.QUORA: client},
            browser_clocks={Platform.QUORA: lambda: NOW},
        )
        result = build_connector_registry(config)[Platform.QUORA].collect(request(Platform.QUORA))
        self.assertEqual(result.status, ConnectorRunStatus.SUCCEEDED)
        self.assertEqual(result.mode, AcquisitionMode.BROWSER)
        self.assertEqual(len(client.submitted), 1)

    def test_unpaired_browser_returns_explicit_blocked_status(self):
        connector = build_connector_registry()[Platform.QUORA]
        result = connector.collect(request(Platform.QUORA))
        self.assertEqual(result.status, ConnectorRunStatus.BLOCKED)
        self.assertIn("not paired", result.reason)

    def test_each_supported_api_platform_accepts_only_explicit_contract(self):
        supported = set(Platform) - {Platform.QUORA}
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "provider.secret"
            secret.write_text("test-only-secret", encoding="utf-8")
            api_routes = {platform: api_config(platform, secret) for platform in supported}
            transports = {
                platform: FakeJSONTransport(
                    {"data": {"items": [{"id": platform.value, "headline": "Item", "text": "Text"}]}}
                )
                for platform in supported
            }
            registry = build_connector_registry(
                ConnectorRuntimeConfig(api_routes=api_routes, api_transports=transports)
            )
            for platform in supported:
                with self.subTest(platform=platform):
                    result = registry[platform].collect(request(platform))
                    self.assertEqual(result.status, ConnectorRunStatus.SUCCEEDED)
                    self.assertEqual(result.mode, AcquisitionMode.API)

    def test_quora_api_contract_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "quora.secret"
            secret.write_text("test-only-secret", encoding="utf-8")
            with self.assertRaises(ConnectorConfigurationError):
                build_connector_registry(
                    ConnectorRuntimeConfig(api_routes={Platform.QUORA: api_config(Platform.QUORA, secret)})
                )

    def test_unsafe_host_and_excessive_timeout_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = SecretFileReference(Path(directory) / "secret")
            common = dict(
                platform=Platform.PINTEREST,
                provider="pinterest-api",
                endpoint_path="search",
                api_version="v1",
                method="GET",
                secret=secret,
                auth_header="Authorization",
                auth_prefix="Bearer ",
                request_field_map={"query": "q"},
                response_items_path=("items",),
                response_field_map={"external_id": ("id",), "title": ("title",)},
            )
            with self.assertRaises(ConnectorConfigurationError):
                JSONAPIRouteConfig(
                    base_url="https://unexpected.example.test",
                    allowed_hosts=("provider.example.test",),
                    **common,
                )
            with self.assertRaises(ConnectorConfigurationError):
                JSONAPIRouteConfig(
                    base_url="https://provider.example.test",
                    allowed_hosts=("provider.example.test",),
                    timeout_seconds=60,
                    **common,
                )

    def test_oversized_provider_response_fails_then_does_not_fake_success(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "gsc.secret"
            secret.write_text("test-only-secret", encoding="utf-8")
            route_config = api_config(Platform.GOOGLE_SEARCH_CONSOLE, secret)
            transport = FakeJSONTransport(
                {"data": {"items": [{"id": "gsc-1", "headline": "Item", "text": "Text"}]}},
                response_bytes=route_config.max_response_bytes + 1,
            )
            connector = build_connector_registry(
                ConnectorRuntimeConfig(
                    api_routes={Platform.GOOGLE_SEARCH_CONSOLE: route_config},
                    api_transports={Platform.GOOGLE_SEARCH_CONSOLE: transport},
                )
            )[Platform.GOOGLE_SEARCH_CONSOLE]
            result = connector.collect(request(Platform.GOOGLE_SEARCH_CONSOLE))
            self.assertEqual(result.status, ConnectorRunStatus.FAILED)
            self.assertIn("byte limit", result.reason)


if __name__ == "__main__":
    unittest.main()
