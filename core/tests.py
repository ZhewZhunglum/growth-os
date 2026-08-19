import uuid

from django.test import TestCase
from django.urls import reverse

from core.ids import uuid7


class UUIDv7Tests(TestCase):
    def test_generated_id_has_expected_version_and_variant(self):
        value = uuid7()
        self.assertIsInstance(value, uuid.UUID)
        self.assertEqual(value.version, 7)
        self.assertEqual(value.variant, uuid.RFC_4122)


class HealthTests(TestCase):
    def test_health_checks_database(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "ok"})

