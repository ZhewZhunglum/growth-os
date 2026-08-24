from __future__ import annotations

from integrations.connectors.types import AvailabilityState, Platform

from .types import (
    PublicationConnectorDescriptor,
    PublicationMode,
    PublicationRouteAvailability,
)


_PROVIDERS: dict[Platform, dict[PublicationMode, str]] = {
    Platform.PINTEREST: {
        PublicationMode.API: "pinterest-content-publish-api",
        PublicationMode.BROWSER: "pinterest-browser-worker",
        PublicationMode.MANUAL: "pinterest-manual-proof",
    },
    Platform.QUORA: {
        PublicationMode.API: "quora-publish-api-unavailable",
        PublicationMode.BROWSER: "quora-browser-worker",
        PublicationMode.MANUAL: "quora-manual-proof",
    },
    Platform.TIKTOK: {
        # TikHub remains a research/collection provider.  It is deliberately
        # not mislabeled as a write API.
        PublicationMode.API: "tiktok-content-posting-api",
        PublicationMode.BROWSER: "tiktok-browser-worker",
        PublicationMode.MANUAL: "tiktok-manual-proof",
    },
    Platform.SHOPIFY: {
        PublicationMode.API: "shopify-admin-api",
        PublicationMode.BROWSER: "shopify-browser-worker",
        PublicationMode.MANUAL: "shopify-manual-proof",
    },
    Platform.GOOGLE_SEARCH: {
        PublicationMode.API: "google-search-publish-api-unavailable",
        PublicationMode.BROWSER: "google-search-browser-worker",
        PublicationMode.MANUAL: "google-search-manual-proof",
    },
    Platform.GOOGLE_SEARCH_CONSOLE: {
        PublicationMode.API: "gsc-publish-api-unavailable",
        PublicationMode.BROWSER: "gsc-browser-worker",
        PublicationMode.MANUAL: "gsc-manual-proof",
    },
    Platform.GOOGLE_ANALYTICS_4: {
        PublicationMode.API: "ga4-publish-api-unavailable",
        PublicationMode.BROWSER: "ga4-browser-worker",
        PublicationMode.MANUAL: "ga4-manual-proof",
    },
}


_API_SUPPORTED = frozenset({Platform.PINTEREST, Platform.TIKTOK, Platform.SHOPIFY})


def default_publication_catalog() -> dict[Platform, PublicationConnectorDescriptor]:
    """Describe the seven V1 surfaces without pretending unsupported APIs exist.

    Browser operations still require an explicitly paired worker.  Manual proof
    is the safe last fallback for every surface.  Mutating routes are never
    selected automatically; the human confirms the exact mode before dispatch.
    """

    catalog: dict[Platform, PublicationConnectorDescriptor] = {}
    for platform in Platform:
        api_state = (
            AvailabilityState.MISSING
            if platform in _API_SUPPORTED
            else AvailabilityState.UNAVAILABLE
        )
        catalog[platform] = PublicationConnectorDescriptor(
            platform=platform,
            routes=(
                PublicationRouteAvailability(
                    mode=PublicationMode.API,
                    state=api_state,
                    provider=_PROVIDERS[platform][PublicationMode.API],
                    priority=1,
                    reason=(
                        "Live API publisher is not configured"
                        if api_state is AvailabilityState.MISSING
                        else "This surface has no supported V1 publication API"
                    ),
                ),
                PublicationRouteAvailability(
                    mode=PublicationMode.BROWSER,
                    state=AvailabilityState.BLOCKED,
                    provider=_PROVIDERS[platform][PublicationMode.BROWSER],
                    priority=2,
                    reason="A paired dedicated browser publisher is required",
                ),
                PublicationRouteAvailability(
                    mode=PublicationMode.MANUAL,
                    state=AvailabilityState.AVAILABLE,
                    provider=_PROVIDERS[platform][PublicationMode.MANUAL],
                    priority=3,
                    reason="A human can publish externally and record URL/content-ID proof",
                ),
            ),
        )
    return catalog
