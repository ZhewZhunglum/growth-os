from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PasswordPolicySettingsTests(SimpleTestCase):
    @staticmethod
    def _secret_file_environment(directory: str) -> dict[str, str]:
        secret_values = {
            "DJANGO_SECRET_KEY_FILE": (
                "settings-test-only-A7x!Q2m#R9v$K4p%T8s&N6d*L3c-W5z"
            ),
            "POSTGRES_PASSWORD_FILE": "settings-test-password",
            "TENCENT_COS_SECRET_ID_FILE": "settings-test-id",
            "TENCENT_COS_SECRET_KEY_FILE": "settings-test-key",
        }
        environment: dict[str, str] = {}
        for name, value in secret_values.items():
            path = Path(directory) / name.lower()
            path.write_text(f"{value}\n", encoding="utf-8")
            environment[name] = str(path)
        return environment

    def _load_settings(self, *, environment: str, minimum: str | None = None):
        process_environment = dict(os.environ)
        for name in (
            "DJANGO_SECRET_KEY",
            "DJANGO_SECRET_KEY_FILE",
            "POSTGRES_PASSWORD",
            "POSTGRES_PASSWORD_FILE",
            "TENCENT_COS_SECRET_ID",
            "TENCENT_COS_SECRET_ID_FILE",
            "TENCENT_COS_SECRET_KEY",
            "TENCENT_COS_SECRET_KEY_FILE",
        ):
            process_environment.pop(name, None)
        process_environment.update(
            {
                "GROWTH_OS_ENV": environment,
                "DJANGO_ALLOWED_HOSTS": "settings.test",
                "DATABASE_ENGINE": "postgresql" if environment != "local" else "sqlite",
                "POSTGRES_DB": "settings_test",
                "POSTGRES_USER": "settings_test",
                "POSTGRES_HOST": "127.0.0.1",
            }
        )
        if environment != "local":
            process_environment.update(
                {
                    "MEDIA_STORAGE_BACKEND": "cos",
                    "TENCENT_COS_BUCKET": "settings-test-1234567890",
                    "TENCENT_COS_REGION": "ap-beijing",
                }
            )
        if minimum is None:
            process_environment.pop("PASSWORD_MIN_LENGTH", None)
        else:
            process_environment["PASSWORD_MIN_LENGTH"] = minimum
        with tempfile.TemporaryDirectory() as secret_directory:
            if environment != "local":
                process_environment.update(
                    self._secret_file_environment(secret_directory)
                )
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
        for name in (
            "DJANGO_SECRET_KEY",
            "DJANGO_SECRET_KEY_FILE",
            "POSTGRES_PASSWORD",
            "POSTGRES_PASSWORD_FILE",
            "TENCENT_COS_SECRET_ID",
            "TENCENT_COS_SECRET_ID_FILE",
            "TENCENT_COS_SECRET_KEY",
            "TENCENT_COS_SECRET_KEY_FILE",
        ):
            process_environment.pop(name, None)
        process_environment.update(
            {
                "GROWTH_OS_ENV": "production",
                "DJANGO_ALLOWED_HOSTS": "settings.test",
                "DATABASE_ENGINE": "postgresql",
                "POSTGRES_DB": "settings_test",
                "POSTGRES_USER": "settings_test",
                "POSTGRES_HOST": "127.0.0.1",
                "MEDIA_STORAGE_BACKEND": "cos",
                "TENCENT_COS_BUCKET": "settings-test-1234567890",
                "TENCENT_COS_REGION": "ap-beijing",
                "TEST_PASSWORD": "            ",
            }
        )
        with tempfile.TemporaryDirectory() as secret_directory:
            process_environment.update(self._secret_file_environment(secret_directory))
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
