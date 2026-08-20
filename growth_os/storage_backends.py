from __future__ import annotations

from functools import cached_property
from typing import Any

from django.conf import settings
from django.core.files import File
from django.core.files.storage import Storage


class TencentCOSPrivateStorage(Storage):
    """Django media storage backed by a private Tencent Cloud COS bucket.

    Only opaque object keys cross the storage boundary.  This backend never
    constructs a public object URL; callers that need downloads must stream
    through an authenticated application endpoint added for that purpose.
    """

    def __init__(
        self,
        *,
        bucket: str | None = None,
        region: str | None = None,
        secret_id: str | None = None,
        secret_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket or settings.TENCENT_COS_BUCKET
        self.region = region or settings.TENCENT_COS_REGION
        self.secret_id = secret_id or settings.TENCENT_COS_SECRET_ID
        self.secret_key = secret_key or settings.TENCENT_COS_SECRET_KEY
        self._injected_client = client

    @cached_property
    def client(self):
        if self._injected_client is not None:
            return self._injected_client

        # Import lazily so Local remains a pure FileSystemStorage environment.
        from qcloud_cos import CosConfig, CosS3Client

        config = CosConfig(
            Region=self.region,
            SecretId=self.secret_id,
            SecretKey=self.secret_key,
            Scheme="https",
        )
        return CosS3Client(config)

    @staticmethod
    def _object_key(name: str) -> str:
        # Django's default collision naming uses os.path.join(), which emits
        # backslashes on Windows. COS keys must be stable POSIX-style strings
        # regardless of the machine running tests or maintenance commands.
        return str(name).replace("\\", "/")

    def get_available_name(self, name: str, max_length: int | None = None) -> str:
        available_name = super().get_available_name(
            self._object_key(name), max_length=max_length
        )
        return self._object_key(available_name)

    def _open(self, name: str, mode: str = "rb") -> File:
        if mode not in {"r", "rb"}:
            raise ValueError("Tencent COS objects can only be opened for reading.")
        object_key = self._object_key(name)
        response = self.client.get_object(Bucket=self.bucket, Key=object_key)
        return File(response["Body"].get_raw_stream(), name=object_key)

    def _save(self, name: str, content) -> str:
        name = self._object_key(name)
        content_type = getattr(content, "content_type", None)

        # ``Storage.save()`` checks availability before calling ``_save()``,
        # but another request can create the same key between those two calls.
        # COS PUT is otherwise an overwrite. The official COS overwrite guard
        # makes this atomic when bucket versioning is disabled; application
        # upload paths are additionally content-addressed so different bytes
        # never share a requested key even when bucket versioning is enabled.
        for _attempt in range(10):
            try:
                content.seek(0)
            except (AttributeError, OSError):
                pass

            request: dict[str, Any] = {
                "Bucket": self.bucket,
                "Key": name,
                "Body": content,
                # Ask COS to validate Content-MD5 in addition to the immutable
                # SHA-256 manifest kept by the application.
                "EnableMD5": True,
                # Explicit object privacy is defense in depth. Deployment must
                # independently verify that the bucket and its policies are also
                # private; an object ACL is not a substitute for that gate.
                "ACL": "private",
                # SDK 1.9.44 accepts arbitrary signed raw headers through the
                # Metadata map. COS returns 409/FileAlreadyExists on collision.
                "Metadata": {"x-cos-forbid-overwrite": "true"},
            }
            if content_type:
                request["ContentType"] = content_type
            try:
                self.client.put_object(**request)
            except Exception as error:
                get_status_code = getattr(error, "get_status_code", None)
                get_error_code = getattr(error, "get_error_code", None)
                try:
                    status_code = int(get_status_code()) if callable(get_status_code) else None
                except (TypeError, ValueError):
                    status_code = None
                error_code = get_error_code() if callable(get_error_code) else None
                if status_code != 409 or error_code != "FileAlreadyExists":
                    raise
                name = self.get_available_name(name)
                continue
            return name

        raise OSError("Unable to reserve a unique private COS object key after 10 attempts.")

    def delete(self, name: str) -> None:
        if name:
            self.client.delete_object(Bucket=self.bucket, Key=self._object_key(name))

    def exists(self, name: str) -> bool:
        name = self._object_key(name)
        try:
            self.client.head_object(Bucket=self.bucket, Key=name)
        except Exception as error:
            # CosServiceError exposes HTTP status through get_status_code().
            # Avoid importing the SDK at module import time so Local does not
            # initialize or require a cloud client.
            get_status_code = getattr(error, "get_status_code", None)
            if callable(get_status_code):
                try:
                    if int(get_status_code()) == 404:
                        return False
                except (TypeError, ValueError):
                    pass
            raise
        return True

    def size(self, name: str) -> int:
        response = self.client.head_object(Bucket=self.bucket, Key=self._object_key(name))
        return int(response["Content-Length"])

    def url(self, name: str) -> str:
        raise NotImplementedError(
            "Public media URLs are disabled for private Tencent COS storage."
        )
