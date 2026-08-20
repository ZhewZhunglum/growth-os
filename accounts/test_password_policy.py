from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PasswordPolicySettingsTests(SimpleTestCase):
    def _load_settings(self, *, environment: str, minimum: str | None = None):
        process_environment = dict(os.environ)
        process_environment.update(
            {
                "GROWTH_OS_ENV": environment,
                "DJANGO_SECRET_KEY": "settings-test-secret-not-for-deployment",
                "DJANGO_ALLOWED_HOSTS": "settings.test",
                "DATABASE_ENGINE": "postgresql" if environment != "local" else "sqlite",
                "POSTGRES_DB": "settings_test",
                "POSTGRES_USER": "settings_test",
                "POSTGRES_PASSWORD": "settings-test-password",
                "POSTGRES_HOST": "127.0.0.1",
            }
        )
        if minimum is None:
            process_environment.pop("PASSWORD_MIN_LENGTH", None)
        else:
            process_environment["PASSWORD_MIN_LENGTH"] = minimum
        return subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import growth_os.settings as configured; "
                    "print(configured.PASSWORD_MIN_LENGTH); "
                    "print(configured.AUTH_PASSWORD_VALIDATORS[2]['OPTIONS']['min_length'])"
                ),
            ],
            cwd=PROJECT_ROOT,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_local_default_is_six_and_configures_django_validator(self):
        self.assertTrue(settings.IS_LOCAL)
        self.assertEqual(settings.PASSWORD_MIN_LENGTH, 6)
        self.assertEqual(settings.AUTH_PASSWORD_VALIDATORS[2]["OPTIONS"]["min_length"], 6)

    def test_local_rejects_whitespace_only_and_control_characters(self):
        with self.assertRaises(ValidationError):
            validate_password("            ")
        with self.assertRaises(ValidationError):
            validate_password("Abcdef\n")

    def test_non_local_default_is_twelve(self):
        result = self._load_settings(environment="staging")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["12", "12"])

    def test_non_local_configuration_cannot_lower_minimum_below_twelve(self):
        result = self._load_settings(environment="staging", minimum="11")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PASSWORD_MIN_LENGTH must be at least 12 in staging", result.stderr)

    def test_non_local_validator_rejects_twelve_whitespace_characters(self):
        process_environment = dict(os.environ)
        process_environment.update(
            {
                "GROWTH_OS_ENV": "production",
                "DJANGO_SECRET_KEY": "settings-test-secret-not-for-deployment",
                "DJANGO_ALLOWED_HOSTS": "settings.test",
                "DATABASE_ENGINE": "postgresql",
                "POSTGRES_DB": "settings_test",
                "POSTGRES_USER": "settings_test",
                "POSTGRES_PASSWORD": "settings-test-password",
                "POSTGRES_HOST": "127.0.0.1",
                "TEST_PASSWORD": "            ",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os; import django; django.setup(); "
                    "from django.contrib.auth.password_validation import validate_password; "
                    "from django.core.exceptions import ValidationError; "
                    "password = os.environ['TEST_PASSWORD']; "
                    "exec(\"try:\\n validate_password(password)\\n print('ACCEPTED')\\nexcept ValidationError:\\n print('REJECTED')\")"
                ),
            ],
            cwd=PROJECT_ROOT,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "REJECTED")

    def test_local_configuration_cannot_lower_minimum_below_six(self):
        result = self._load_settings(environment="local", minimum="5")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PASSWORD_MIN_LENGTH must be at least 6 in local", result.stderr)
