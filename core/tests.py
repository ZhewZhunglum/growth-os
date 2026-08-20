import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from django.test import SimpleTestCase, override_settings
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
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "database": "ok",
                "deployment": {"stage": "local", "revision": "unknown"},
            },
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    @override_settings(
        DEPLOYMENT_STAGE="staging-candidate",
        RELEASE_SHA="c91160c3b8baec39d955c38e41ee1995af900c3e",
    )
    def test_health_exposes_only_minimal_traceable_deployment_identity(self):
        response = self.client.get(reverse("health"))

        self.assertEqual(
            response.json()["deployment"],
            {
                "stage": "staging-candidate",
                "revision": "c91160c3b8baec39d955c38e41ee1995af900c3e",
            },
        )
        serialized = response.content.decode("utf-8").lower()
        for forbidden in ("password", "secret", "token", "postgres_host", "allowed_hosts"):
            self.assertNotIn(forbidden, serialized)


class DeploymentIdentitySettingsTests(SimpleTestCase):
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

    def _load_settings(self, **overrides):
        process_environment = dict(os.environ)
        process_environment.update(
            {
                "GROWTH_OS_ENV": "local",
                "DATABASE_ENGINE": "sqlite",
                **overrides,
            }
        )
        return subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import growth_os.settings as configured; "
                    "print(configured.DEPLOYMENT_STAGE); "
                    "print(configured.RELEASE_SHA)"
                ),
            ],
            cwd=self.PROJECT_ROOT,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_full_lowercase_sha_and_controlled_stage_are_accepted(self):
        revision = "c91160c3b8baec39d955c38e41ee1995af900c3e"
        result = self._load_settings(
            GROWTH_OS_DEPLOYMENT_STAGE="staging-candidate",
            GROWTH_OS_RELEASE_SHA=revision,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["staging-candidate", revision])

    def test_missing_sha_uses_honest_unknown_default(self):
        process_environment = dict(os.environ)
        process_environment.pop("GROWTH_OS_RELEASE_SHA", None)
        process_environment.update({"GROWTH_OS_ENV": "local", "DATABASE_ENGINE": "sqlite"})
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import growth_os.settings as configured; print(configured.RELEASE_SHA)",
            ],
            cwd=self.PROJECT_ROOT,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "unknown")

    def test_abbreviated_or_untrusted_sha_is_rejected(self):
        result = self._load_settings(GROWTH_OS_RELEASE_SHA="c91160c")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full 40-character lowercase Git SHA", result.stderr)

    def test_uncontrolled_stage_value_is_rejected(self):
        result = self._load_settings(GROWTH_OS_DEPLOYMENT_STAGE="staging\r\nX-Forged: yes")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GROWTH_OS_DEPLOYMENT_STAGE must be", result.stderr)

    def test_misspelled_runtime_environment_fails_closed(self):
        result = self._load_settings(GROWTH_OS_ENV="prodution")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GROWTH_OS_ENV must be local, staging, or production", result.stderr)

    def test_staging_uses_secure_cookies_and_https_redirect(self):
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
                "GROWTH_OS_ENV": "staging",
                "GROWTH_OS_DEPLOYMENT_STAGE": "staging-candidate",
                "DATABASE_ENGINE": "postgresql",
                "DJANGO_ALLOWED_HOSTS": "staging.example.test",
                "DJANGO_CSRF_TRUSTED_ORIGINS": "https://staging.example.test",
                "POSTGRES_DB": "growth_os_test",
                "POSTGRES_USER": "growth_os_test",
                "POSTGRES_HOST": "127.0.0.1",
                "MEDIA_STORAGE_BACKEND": "cos",
                "TENCENT_COS_BUCKET": "settings-test-1234567890",
                "TENCENT_COS_REGION": "ap-beijing",
            }
        )
        with tempfile.TemporaryDirectory() as secret_directory:
            secret_values = {
                "DJANGO_SECRET_KEY_FILE": (
                    "staging-test-only-A7x!Q2m#R9v$K4p%T8s&N6d*L3c-W5z-X9Z"
                ),
                "POSTGRES_PASSWORD_FILE": "test-only-not-a-real-secret",
                "TENCENT_COS_SECRET_ID_FILE": "settings-test-id",
                "TENCENT_COS_SECRET_KEY_FILE": "settings-test-key",
            }
            for name, value in secret_values.items():
                path = Path(secret_directory) / name.lower()
                path.write_text(f"{value}\n", encoding="utf-8")
                process_environment[name] = str(path)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import growth_os.settings as configured; "
                        "print(configured.SECURE_SSL_REDIRECT); "
                        "print(configured.SESSION_COOKIE_SECURE); "
                        "print(configured.CSRF_COOKIE_SECURE); "
                        "print(hasattr(configured, 'SECURE_HSTS_SECONDS'))"
                    ),
                ],
                cwd=self.PROJECT_ROOT,
                env=process_environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["True", "True", "True", "False"])

