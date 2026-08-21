from __future__ import annotations

import unittest

from integrations.connectors import (
    AcquisitionMode,
    AvailabilityState,
    Platform,
    default_connector_catalog,
)


class ConnectorCatalogTests(unittest.TestCase):
    def test_catalog_contains_all_seven_platforms_and_four_routes(self):
        catalog = default_connector_catalog()
        self.assertEqual(set(catalog), set(Platform))
        for descriptor in catalog.values():
            self.assertEqual({route.mode for route in descriptor.routes}, set(AcquisitionMode))

    def test_tikhub_is_tiktok_primary_and_csv_is_default_available_fallback(self):
        descriptor = default_connector_catalog()[Platform.TIKTOK]
        self.assertEqual(descriptor.routes[0].provider, "tikhub-api")
        self.assertEqual(descriptor.routes[0].state, AvailabilityState.MISSING)
        self.assertEqual(descriptor.next_available_route.mode, AcquisitionMode.CSV)

    def test_quora_explicitly_marks_api_unavailable_and_browser_primary(self):
        descriptor = default_connector_catalog()[Platform.QUORA]
        self.assertEqual(descriptor.routes[0].mode, AcquisitionMode.BROWSER)
        api = next(route for route in descriptor.routes if route.mode is AcquisitionMode.API)
        self.assertEqual(api.state, AvailabilityState.UNAVAILABLE)

    def test_browser_pairing_override_makes_browser_available(self):
        catalog = default_connector_catalog(
            {Platform.PINTEREST: {AcquisitionMode.BROWSER: AvailabilityState.AVAILABLE}}
        )
        descriptor = catalog[Platform.PINTEREST]
        browser = next(route for route in descriptor.routes if route.mode is AcquisitionMode.BROWSER)
        self.assertEqual(browser.state, AvailabilityState.AVAILABLE)


if __name__ == "__main__":
    unittest.main()
