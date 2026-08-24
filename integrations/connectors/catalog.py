from __future__ import annotations

from typing import Mapping

from integrations.connectors.types import (
    AcquisitionMode,
    AvailabilityState,
    ConnectorDescriptor,
    Platform,
    RouteAvailability,
)


_PROVIDERS: dict[Platform, dict[AcquisitionMode, str]] = {
    Platform.PINTEREST: {
        AcquisitionMode.API: "pinterest-api",
        AcquisitionMode.BROWSER: "pinterest-browser-worker",
        AcquisitionMode.CSV: "pinterest-csv",
        AcquisitionMode.MANUAL: "pinterest-manual-link",
    },
    Platform.QUORA: {
        AcquisitionMode.API: "quora-research-api-unavailable",
        AcquisitionMode.BROWSER: "quora-browser-worker",
        AcquisitionMode.CSV: "quora-csv",
        AcquisitionMode.MANUAL: "quora-manual-link",
    },
    Platform.TIKTOK: {
        AcquisitionMode.API: "tikhub-api",
        AcquisitionMode.BROWSER: "tiktok-creative-center-browser-worker",
        AcquisitionMode.CSV: "tiktok-csv",
        AcquisitionMode.MANUAL: "tiktok-manual-link",
    },
    Platform.SHOPIFY: {
        AcquisitionMode.API: "shopify-admin-api",
        AcquisitionMode.BROWSER: "shopify-browser-worker",
        AcquisitionMode.CSV: "shopify-csv",
        AcquisitionMode.MANUAL: "shopify-manual-link",
    },
    Platform.GOOGLE_SEARCH: {
        AcquisitionMode.API: "google-search-api",
        AcquisitionMode.BROWSER: "google-search-browser-worker",
        AcquisitionMode.CSV: "google-search-csv",
        AcquisitionMode.MANUAL: "google-search-manual-link",
    },
    Platform.GOOGLE_SEARCH_CONSOLE: {
        AcquisitionMode.API: "google-search-console-api",
        AcquisitionMode.BROWSER: "gsc-browser-worker",
        AcquisitionMode.CSV: "gsc-csv",
        AcquisitionMode.MANUAL: "gsc-manual-link",
    },
    Platform.GOOGLE_ANALYTICS_4: {
        AcquisitionMode.API: "ga4-data-api",
        AcquisitionMode.BROWSER: "ga4-browser-worker",
        AcquisitionMode.CSV: "ga4-csv",
        AcquisitionMode.MANUAL: "ga4-manual-link",
    },
}


def default_connector_catalog(
    overrides: Mapping[Platform, Mapping[AcquisitionMode, AvailabilityState]] | None = None,
) -> dict[Platform, ConnectorDescriptor]:
    """Return a fail-closed catalogue with usable offline fallbacks.

    API credentials are missing and browser pairing is blocked by default.
    CSV and manual ingestion are locally available.  Quora's research API route
    is explicitly unavailable rather than silently pretending an API exists.
    """

    overrides = overrides or {}
    catalog: dict[Platform, ConnectorDescriptor] = {}
    for platform in Platform:
        defaults = {
            AcquisitionMode.API: (
                AvailabilityState.UNAVAILABLE
                if platform is Platform.QUORA
                else AvailabilityState.MISSING
            ),
            AcquisitionMode.BROWSER: AvailabilityState.BLOCKED,
            AcquisitionMode.CSV: AvailabilityState.AVAILABLE,
            AcquisitionMode.MANUAL: AvailabilityState.AVAILABLE,
        }
        defaults.update(overrides.get(platform, {}))
        route_order = _route_order(platform)
        routes = tuple(
            RouteAvailability(
                mode=mode,
                state=defaults[mode],
                provider=_PROVIDERS[platform][mode],
                priority=index,
                reason=_availability_reason(defaults[mode], mode),
            )
            for index, mode in enumerate(route_order, start=1)
        )
        catalog[platform] = ConnectorDescriptor(platform=platform, routes=routes)
    return catalog


def _route_order(platform: Platform) -> tuple[AcquisitionMode, ...]:
    if platform is Platform.QUORA:
        return (
            AcquisitionMode.BROWSER,
            AcquisitionMode.CSV,
            AcquisitionMode.MANUAL,
            AcquisitionMode.API,
        )
    return (
        AcquisitionMode.API,
        AcquisitionMode.BROWSER,
        AcquisitionMode.CSV,
        AcquisitionMode.MANUAL,
    )


def _availability_reason(state: AvailabilityState, mode: AcquisitionMode) -> str:
    if state is AvailabilityState.AVAILABLE:
        return f"{mode.value} route is locally available"
    if state is AvailabilityState.MISSING:
        return f"{mode.value} route is not configured"
    if state is AvailabilityState.BLOCKED:
        return f"{mode.value} route requires a paired dedicated browser profile"
    return f"{mode.value} route is not supported for this platform"
