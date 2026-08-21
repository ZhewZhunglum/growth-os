from django.test import SimpleTestCase

from dailyops.platform_detection import detect_platform
from integrations.connectors.types import Platform


class PlatformDetectionTests(SimpleTestCase):
    def test_supported_platform_urls_are_detected(self):
        cases = {
            "https://www.pinterest.com/pin/123/": Platform.PINTEREST,
            "pin.it/abc123": Platform.PINTEREST,
            "https://www.quora.com/How-do-I-focus": Platform.QUORA,
            "https://www.tiktok.com/@puko/video/123": Platform.TIKTOK,
            "https://admin.shopify.com/store/puko": Platform.SHOPIFY,
            "https://puko.myshopify.com/products/example": Platform.SHOPIFY,
            "https://www.google.com/search?q=puko": Platform.GOOGLE_SEARCH,
            "https://search.google.com/search-console/performance/search-analytics": (
                Platform.GOOGLE_SEARCH_CONSOLE
            ),
            "https://analytics.google.com/analytics/web/": Platform.GOOGLE_ANALYTICS_4,
        }
        for reference, expected in cases.items():
            with self.subTest(reference=reference):
                detection = detect_platform(reference)
                self.assertTrue(detection.is_url)
                self.assertEqual(detection.platform, expected)

    def test_unknown_url_requires_explicit_platform(self):
        detection = detect_platform("https://example.com/post/123")
        self.assertTrue(detection.is_url)
        self.assertIsNone(detection.platform)
    def test_non_url_reference_is_not_guessed(self):
        detection = detect_platform("video-123")
        self.assertFalse(detection.is_url)
        self.assertIsNone(detection.platform)
