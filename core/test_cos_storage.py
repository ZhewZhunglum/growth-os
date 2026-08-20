from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.files.base import ContentFile
from django.test import SimpleTestCase

from growth_os.storage_backends import TencentCOSPrivateStorage


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COS_ENVIRONMENT_NAMES = {
    "DJANGO_SECRET_KEY",
    "DJANGO_SECRET_KEY_FILE",
    "MEDIA_STORAGE_BACKEND",
    "POSTGRES_PASSWORD",
    "POSTGRES_PASSWORD_FILE",
    "TENCENT_COS_BUCKET",
    "TENCENT_COS_REGION",
    "TENCENT_COS_SECRET_ID",
    "TENCENT_COS_SECRET_ID_FILE",
    "TENCENT_COS_SECRET_KEY",
    "TENCENT_COS_SECRET_KEY_FILE",
}


class MissingCOSObject(Exception):
    def get_status_code(self):
        return 404


class COSObjectAlreadyExists(Exception):
    def get_status_code(self):
        return 409

    def get_error_code(self):
        return "FileAlreadyExists"


class COSUploadConflict(Exception):
    def get_status_code(self):
        return 409

    def get_error_code(self):
        return "UploadConflict"


class FakeCOSBody:
    def __init__(self, payload: bytes):
        self.payload = payload

    def get_raw_stream(self):
        return io.BytesIO(self.payload)


class FakeCOSClient:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.put_requests: list[dict] = []
        self.deleted: list[dict] = []

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise MissingCOSObject(Key)
        return {"Content-Length": str(len(self.objects[Key]))}

    def put_object(self, **request):
        forbid_overwrite = request.get("Metadata", {}).get(
            "x-cos-forbid-overwrite"
        )
        if forbid_overwrite == "true" and request["Key"] in self.objects:
            raise COSObjectAlreadyExists(request["Key"])
        payload = request["Body"].read()
        self.objects[request["Key"]] = payload
        self.put_requests.append({**request, "Body": payload})
        return {"ETag": "test-only"}

    def get_object(self, *, Bucket, Key):
        return {"Body": FakeCOSBody(self.objects[Key])}

    def delete_object(self, **request):
        self.objects.pop(request["Key"], None)
        self.deleted.append(request)


class TencentCOSPrivateStorageTests(SimpleTestCase):
    def storage(self, client):
        return TencentCOSPrivateStorage(
            bucket="private-media-1234567890",
            region="ap-beijing",
            secret_id="test-secret-id",
            secret_key="test-secret-key",
            client=client,
        )

    def test_save_returns_actual_collision_safe_object_key_and_forces_private_acl(self):
        requested = "task-deliveries/task/file.txt"
        client = FakeCOSClient({requested: b"existing"})
        storage = self.storage(client)

        actual = storage.save(requested, ContentFile(b"new payload"))

        self.assertNotEqual(actual, requested)
        self.assertTrue(actual.startswith("task-deliveries/task/file_"))
        self.assertEqual(client.put_requests[0]["Key"], actual)
        self.assertEqual(client.put_requests[0]["ACL"], "private")
        self.assertEqual(
            client.put_requests[0]["Metadata"],
            {"x-cos-forbid-overwrite": "true"},
        )
        self.assertIs(client.put_requests[0]["EnableMD5"], True)
        self.assertEqual(client.put_requests[0]["Body"], b"new payload")

    def test_racing_writer_cannot_overwrite_existing_immutable_object(self):
        requested = "task-deliveries/task/command/file.txt"
        client = FakeCOSClient()
        storage = self.storage(client)

        # Simulate another writer winning after Django's availability check but
        # immediately before this request reaches COS.
        original_put = client.put_object
        first_attempt = True

        def racing_put(**request):
            nonlocal first_attempt
            if first_attempt:
                first_attempt = False
                client.objects[request["Key"]] = b"winner bytes"
            return original_put(**request)

        client.put_object = racing_put

        actual = storage.save(requested, ContentFile(b"losing request bytes"))

        self.assertNotEqual(actual, requested)
        self.assertEqual(client.objects[requested], b"winner bytes")
        self.assertEqual(client.objects[actual], b"losing request bytes")

    def test_non_file_exists_conflict_is_not_misreported_as_a_name_collision(self):
        client = FakeCOSClient()
        client.put_object = Mock(side_effect=COSUploadConflict("locked upload"))
        storage = self.storage(client)

        with self.assertRaises(COSUploadConflict):
            storage.save(
                "task-deliveries/task/command/file.txt",
                ContentFile(b"payload"),
            )

    def test_private_storage_supports_server_side_read_size_exists_and_delete(self):
        key = "task-deliveries/task/file.txt"
        client = FakeCOSClient({key: b"controlled content"})
        storage = self.storage(client)

        self.assertTrue(storage.exists(key))
        self.assertFalse(storage.exists("missing.txt"))
        self.assertEqual(storage.size(key), len(b"controlled content"))
        with storage.open(key, "rb") as stored_file:
            self.assertEqual(stored_file.read(), b"controlled content")

        storage.delete(key)
        self.assertFalse(storage.exists(key))
        self.assertEqual(
            client.deleted,
            [{"Bucket": "private-media-1234567890", "Key": key}],
        )

    def test_public_url_generation_is_disabled(self):
        storage = self.storage(FakeCOSClient())

        with self.assertRaisesMessage(NotImplementedError, "Public media URLs are disabled"):
            storage.url("task-deliveries/task/file.txt")

    def test_sdk_client_is_https_only(self):
        fake_module = ModuleType("qcloud_cos")
        fake_config = Mock(side_effect=lambda **values: values)
        expected_client = object()
        fake_client_factory = Mock(return_value=expected_client)
        fake_module.CosConfig = fake_config
        fake_module.CosS3Client = fake_client_factory
        storage = self.storage(client=None)
        storage._injected_client = None

        with patch.dict(sys.modules, {"qcloud_cos": fake_module}):
            self.assertIs(storage.client, expected_client)

        fake_config.assert_called_once_with(
            Region="ap-beijing",
            SecretId="test-secret-id",
            SecretKey="test-secret-key",
            Scheme="https",
        )

    def test_pinned_sdk_maps_official_forbid_overwrite_header(self):
        from qcloud_cos.cos_comm import mapped

        self.assertEqual(
            mapped({"Metadata": {"x-cos-forbid-overwrite": "true"}})[
                "x-cos-forbid-overwrite"
            ],
            "true",
        )


class MediaStorageSettingsTests(SimpleTestCase):
    def _environment(self, **overrides):
        environment = dict(os.environ)
        for name in COS_ENVIRONMENT_NAMES:
            environment.pop(name, None)
        environment.update(
            {
                "GROWTH_OS_ENV": "staging",
                "GROWTH_OS_DEPLOYMENT_STAGE": "staging-candidate",
                "DJANGO_ALLOWED_HOSTS": "staging.example.test",
                "DATABASE_ENGINE": "postgresql",
                "POSTGRES_DB": "settings_test",
                "POSTGRES_USER": "settings_test",
                "POSTGRES_HOST": "127.0.0.1",
                **overrides,
            }
        )
        return environment

    def _load_settings(self, environment):
        with tempfile.TemporaryDirectory() as temporary_directory:
            defaults = {
                "DJANGO_SECRET_KEY_FILE": (
                    "settings-test-only-A7x!Q2m#R9v$K4p%T8s&N6d*L3c-W5z"
                ),
                "POSTGRES_PASSWORD_FILE": "settings-test-password",
                "TENCENT_COS_SECRET_ID_FILE": "test-secret-id",
                "TENCENT_COS_SECRET_KEY_FILE": "test-secret-key",
            }
            direct_names = {
                "DJANGO_SECRET_KEY_FILE": "DJANGO_SECRET_KEY",
                "POSTGRES_PASSWORD_FILE": "POSTGRES_PASSWORD",
                "TENCENT_COS_SECRET_ID_FILE": "TENCENT_COS_SECRET_ID",
                "TENCENT_COS_SECRET_KEY_FILE": "TENCENT_COS_SECRET_KEY",
            }
            for file_name, value in defaults.items():
                if file_name in environment or direct_names[file_name] in environment:
                    continue
                path = Path(temporary_directory) / file_name.lower()
                path.write_text(f"{value}\n", encoding="utf-8")
                environment[file_name] = str(path)
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import growth_os.settings as configured; "
                        "print(configured.STORAGES['default']['BACKEND']); "
                        "print(configured.TENCENT_COS_SECRET_ID == 'test-secret-id'); "
                        "print(configured.TENCENT_COS_SECRET_KEY == 'test-secret-key')"
                    ),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_local_uses_filesystem_storage_without_cos_configuration(self):
        self.assertTrue(settings.IS_LOCAL)
        self.assertEqual(settings.MEDIA_STORAGE_BACKEND, "filesystem")
        self.assertEqual(
            settings.STORAGES["default"]["BACKEND"],
            "django.core.files.storage.FileSystemStorage",
        )
        self.assertEqual(settings.LOGGING["loggers"]["qcloud_cos"]["level"], "WARNING")

    def test_staging_requires_explicit_cos_backend(self):
        result = self._load_settings(self._environment())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("require explicit MEDIA_STORAGE_BACKEND=cos", result.stderr)

    def test_staging_reads_cos_credentials_from_secret_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            secret_id_path = Path(temporary_directory) / "secret-id"
            secret_key_path = Path(temporary_directory) / "secret-key"
            secret_id_path.write_text("test-secret-id\n", encoding="utf-8")
            secret_key_path.write_text("test-secret-key\n", encoding="utf-8")
            result = self._load_settings(
                self._environment(
                    MEDIA_STORAGE_BACKEND="cos",
                    TENCENT_COS_BUCKET="private-media-1234567890",
                    TENCENT_COS_REGION="ap-beijing",
                    TENCENT_COS_SECRET_ID_FILE=str(secret_id_path),
                    TENCENT_COS_SECRET_KEY_FILE=str(secret_key_path),
                )
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "growth_os.storage_backends.TencentCOSPrivateStorage",
                "True",
                "True",
            ],
        )

    def test_staging_reads_django_database_and_cos_secrets_from_files_without_printing_them(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            secret_directory = Path(temporary_directory)
            secret_values = {
                "django": "django-file-only-A7x!Q2m#R9v$K4p%T8s&N6d*L3c-W5z-X9Z",
                "postgres": "postgres-file-secret",
                "cos-id": "test-secret-id",
                "cos-key": "test-secret-key",
            }
            secret_paths = {}
            for label, value in secret_values.items():
                path = secret_directory / label
                path.write_text(f"{value}\n", encoding="utf-8")
                secret_paths[label] = path

            environment = self._environment(
                MEDIA_STORAGE_BACKEND="cos",
                TENCENT_COS_BUCKET="private-media-1234567890",
                TENCENT_COS_REGION="ap-beijing",
                DJANGO_SECRET_KEY_FILE=str(secret_paths["django"]),
                POSTGRES_PASSWORD_FILE=str(secret_paths["postgres"]),
                TENCENT_COS_SECRET_ID_FILE=str(secret_paths["cos-id"]),
                TENCENT_COS_SECRET_KEY_FILE=str(secret_paths["cos-key"]),
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import growth_os.settings as configured; "
                        "assert configured.SECRET_KEY == "
                        "'django-file-only-A7x!Q2m#R9v$K4p%T8s&N6d*L3c-W5z-X9Z'; "
                        "assert configured.DATABASES['default']['PASSWORD'] == 'postgres-file-secret'; "
                        "assert configured.TENCENT_COS_SECRET_ID == 'test-secret-id'; "
                        "assert configured.TENCENT_COS_SECRET_KEY == 'test-secret-key'; "
                        "print('SECRET_FILE_SETTINGS_OK')"
                    ),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "SECRET_FILE_SETTINGS_OK")
        for secret in secret_values.values():
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)

    def test_staging_rejects_each_direct_secret_environment_value(self):
        direct_secrets = {
            "DJANGO_SECRET_KEY": "direct-django-secret",
            "POSTGRES_PASSWORD": "direct-postgres-secret",
            "TENCENT_COS_SECRET_ID": "direct-cos-id",
            "TENCENT_COS_SECRET_KEY": "direct-cos-key",
        }
        for secret_name, secret_value in direct_secrets.items():
            with self.subTest(secret_name=secret_name):
                result = self._load_settings(
                    self._environment(
                        MEDIA_STORAGE_BACKEND="cos",
                        TENCENT_COS_BUCKET="private-media-1234567890",
                        TENCENT_COS_REGION="ap-beijing",
                        **{secret_name: secret_value},
                    )
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    f"{secret_name} must use the {secret_name}_FILE secret mount",
                    result.stderr,
                )
                self.assertNotIn(secret_value, result.stdout)
                self.assertNotIn(secret_value, result.stderr)

    def test_staging_rejects_a_weak_secret_key_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            weak_secret_path = Path(temporary_directory) / "weak-django-secret"
            weak_secret_path.write_text("too-short\n", encoding="utf-8")
            result = self._load_settings(
                self._environment(
                    MEDIA_STORAGE_BACKEND="cos",
                    TENCENT_COS_BUCKET="private-media-1234567890",
                    TENCENT_COS_REGION="ap-beijing",
                    DJANGO_SECRET_KEY_FILE=str(weak_secret_path),
                )
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "DJANGO_SECRET_KEY_FILE must contain a strong deployment signing key",
            result.stderr,
        )
        self.assertNotIn("too-short", result.stdout)
        self.assertNotIn("too-short", result.stderr)

    def test_staging_rejects_ambiguous_direct_and_file_secret(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            secret_id_path = Path(temporary_directory) / "secret-id"
            secret_id_path.write_text("test-secret-id\n", encoding="utf-8")
            result = self._load_settings(
                self._environment(
                    MEDIA_STORAGE_BACKEND="cos",
                    TENCENT_COS_BUCKET="private-media-1234567890",
                    TENCENT_COS_REGION="ap-beijing",
                    TENCENT_COS_SECRET_ID="test-secret-id",
                    TENCENT_COS_SECRET_ID_FILE=str(secret_id_path),
                    TENCENT_COS_SECRET_KEY="test-secret-key",
                )
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Set only one of TENCENT_COS_SECRET_ID or TENCENT_COS_SECRET_ID_FILE",
            result.stderr,
        )
