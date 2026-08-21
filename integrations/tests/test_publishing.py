from __future__ import annotations

import unittest

from integrations.connectors.types import AvailabilityState, Platform
from integrations.publishing import (
    DryRunPublicationTransport,
    PublicationDispatchRequest,
    PublicationDispatchResult,
    PublicationDispatchStatus,
    PublicationMode,
    PublicationRuntime,
    PublicationRuntimeConfig,
    default_publication_catalog,
)


def request(
    platform: Platform = Platform.TIKTOK,
    mode: PublicationMode = PublicationMode.API,
) -> PublicationDispatchRequest:
    return PublicationDispatchRequest(
        platform=platform,
        mode=mode,
        operation_key="publish:daily:123",
        account_ref="puko-us",
        asset_version_id="asset-version-1",
        asset_external_url="https://drafts.example.com/puko/v1",
        gate_id="gate-1",
        gate_context_sha256="a" * 64,
        human_confirmation_id="confirmation-1",
        confirmed_by_principal_id="principal-1",
    )


class _SuccessfulTransport:
    def __init__(self):
        self.calls: list[PublicationDispatchRequest] = []

    def dispatch(self, value: PublicationDispatchRequest) -> PublicationDispatchResult:
        self.calls.append(value)
        return PublicationDispatchResult(
            platform=value.platform,
            mode=value.mode,
            provider="fake-tiktok-publisher",
            status=PublicationDispatchStatus.SUCCEEDED,
            operation_key=value.operation_key,
            external_publication_id="video-123",
            external_url="https://www.tiktok.com/@puko/video/123",
        )


class PublicationConnectorTests(unittest.TestCase):
    def test_catalog_declares_api_browser_manual_for_all_seven_platforms(self):
        catalog = default_publication_catalog()
        self.assertEqual(set(catalog), set(Platform))
        for descriptor in catalog.values():
            self.assertEqual({route.mode for route in descriptor.routes}, set(PublicationMode))
            self.assertEqual(descriptor.route(PublicationMode.MANUAL).state, AvailabilityState.AVAILABLE)

    def test_tikhub_is_not_mislabeled_as_a_tiktok_write_api(self):
        route = default_publication_catalog()[Platform.TIKTOK].route(PublicationMode.API)
        self.assertEqual(route.provider, "tiktok-content-posting-api")
        self.assertNotIn("tikhub", route.provider)

    def test_nonpublishing_google_and_quora_api_routes_are_explicitly_unavailable(self):
        catalog = default_publication_catalog()
        for platform in (
            Platform.QUORA,
            Platform.GOOGLE_SEARCH,
            Platform.GOOGLE_SEARCH_CONSOLE,
            Platform.GOOGLE_ANALYTICS_4,
        ):
            self.assertEqual(
                catalog[platform].route(PublicationMode.API).state,
                AvailabilityState.UNAVAILABLE,
            )

    def test_default_runtime_is_disabled_and_never_fakes_success(self):
        result = PublicationRuntime().dispatch(request())
        self.assertEqual(result.status, PublicationDispatchStatus.BLOCKED)
        self.assertIn("not configured", result.reason)

    def test_explicit_transport_binds_exact_request_and_result(self):
        transport = _SuccessfulTransport()
        runtime = PublicationRuntime(
            PublicationRuntimeConfig({(Platform.TIKTOK, PublicationMode.API): transport})
        )
        result = runtime.dispatch(request())
        self.assertEqual(result.status, PublicationDispatchStatus.SUCCEEDED)
        self.assertEqual(result.external_publication_id, "video-123")
        self.assertEqual(len(transport.calls), 1)

    def test_dry_run_is_offline_and_never_returns_external_proof(self):
        runtime = PublicationRuntime(
            PublicationRuntimeConfig(
                {(Platform.TIKTOK, PublicationMode.API): DryRunPublicationTransport()}
            )
        )
        result = runtime.dispatch(request())
        self.assertEqual(result.status, PublicationDispatchStatus.DRY_RUN)
        self.assertFalse(result.external_url)
        self.assertFalse(result.external_publication_id)

    def test_response_mismatch_fails_closed(self):
        class WrongPlatformTransport:
            def dispatch(self, value):
                return PublicationDispatchResult(
                    platform=Platform.PINTEREST,
                    mode=value.mode,
                    provider="bad-fake",
                    status=PublicationDispatchStatus.SUCCEEDED,
                    operation_key=value.operation_key,
                    external_publication_id="wrong-1",
                )

        runtime = PublicationRuntime(
            PublicationRuntimeConfig(
                {(Platform.TIKTOK, PublicationMode.API): WrongPlatformTransport()}
            )
        )
        result = runtime.dispatch(request())
        self.assertEqual(result.status, PublicationDispatchStatus.FAILED)
        self.assertIn("exact confirmed request", result.reason)


if __name__ == "__main__":
    unittest.main()
