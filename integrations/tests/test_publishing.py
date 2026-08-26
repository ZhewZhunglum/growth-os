from __future__ import annotations

import unittest

from integrations.connectors.types import AvailabilityState, Platform
from integrations.publishing import (
    DryRunPublicationTransport,
    PublicationAssetRepresentation,
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
    representation_kind: PublicationAssetRepresentation = (
        PublicationAssetRepresentation.EXTERNAL_URL
    ),
) -> PublicationDispatchRequest:
    is_inline = representation_kind is PublicationAssetRepresentation.INLINE_TEXT
    return PublicationDispatchRequest(
        platform=platform,
        mode=mode,
        operation_key="publish:daily:123",
        account_ref="puko-us",
        asset_version_id="asset-version-1",
        asset_representation_kind=representation_kind,
        asset_external_url="" if is_inline else "https://drafts.example.com/puko/v1",
        asset_inline_content="A complete TikTok post ready to publish." if is_inline else "",
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

    def test_inline_text_is_passed_to_api_and_browser_as_the_exact_approved_asset(self):
        for mode in (PublicationMode.API, PublicationMode.BROWSER):
            with self.subTest(mode=mode):
                transport = _SuccessfulTransport()
                runtime = PublicationRuntime(
                    PublicationRuntimeConfig({(Platform.TIKTOK, mode): transport})
                )
                result = runtime.dispatch(
                    request(
                        mode=mode,
                        representation_kind=PublicationAssetRepresentation.INLINE_TEXT,
                    )
                )
                self.assertEqual(result.status, PublicationDispatchStatus.SUCCEEDED)
                dispatched = transport.calls[0]
                self.assertEqual(
                    dispatched.asset_representation_kind,
                    PublicationAssetRepresentation.INLINE_TEXT,
                )
                self.assertEqual(
                    dispatched.asset_inline_content,
                    "A complete TikTok post ready to publish.",
                )
                self.assertEqual(dispatched.asset_external_url, "")

    def test_asset_envelope_requires_exactly_one_matching_representation(self):
        base = request()
        values = {
            field: getattr(base, field)
            for field in base.__dataclass_fields__
        }
        values.update(
            asset_representation_kind=PublicationAssetRepresentation.INLINE_TEXT,
            asset_external_url="https://drafts.example.com/puko/v1",
            asset_inline_content="inline",
        )
        with self.assertRaisesRegex(ValueError, "cannot also contain an external URL"):
            PublicationDispatchRequest(**values)

        values.update(asset_external_url="", asset_inline_content="   ")
        with self.assertRaisesRegex(ValueError, "non-blank content"):
            PublicationDispatchRequest(**values)

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
