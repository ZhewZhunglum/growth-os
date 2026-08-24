from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from integrations.ai.secrets import SecretFileReference
from integrations.browser_worker import (
    BrowserJobOperation,
    BrowserWorkerJob,
    BrowserWorkerJobStatus,
    HTTPBrowserWorkerClient,
    HTTPBrowserWorkerConfig,
)
from integrations.connectors import Platform
from integrations.errors import BrowserWorkerProtocolError, NetworkAccessDisabled


NOW = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload, *, status=200, url=None):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.status = status
        self.url = url
        self.headers = {"content-type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit):
        return self.body[:limit]

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError("Unexpected fake network request")
        response = self.responses.pop(0)
        if response.url is None:
            response.url = request.full_url
        return response


class HTTPBrowserWorkerClientTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        secret_path = Path(self.temp_dir.name) / "browser-worker-token"
        secret_path.write_text("paired-worker-secret", encoding="utf-8")
        self.secret = SecretFileReference(secret_path)

    def config(self, **overrides):
        values = {
            "base_url": "https://worker.example/api/v1",
            "secret": self.secret,
            "allowed_worker_hosts": ("worker.example",),
            "timeout_seconds": 4,
            "max_request_bytes": 20_000,
            "max_response_bytes": 20_000,
        }
        values.update(overrides)
        return HTTPBrowserWorkerConfig(**values)

    def job(self, **overrides):
        values = {
            "job_id": uuid.uuid4(),
            "operation_key": "daily:2026-08-21:tiktok",
            "platform": Platform.TIKTOK,
            "operation": BrowserJobOperation.SEARCH,
            "pairing_id": uuid.uuid4(),
            "dedicated_profile_id": "profile-1",
            "created_at": NOW,
            "expires_at": NOW + timedelta(minutes=15),
            "query": "focus supplement",
            "max_items": 20,
            "allowed_hosts": ("tiktok.com",),
            "payload": {"market_code": "US"},
        }
        values.update(overrides)
        return BrowserWorkerJob(**values)

    @staticmethod
    def binding(job, **extra):
        return {
            "job_id": str(job.job_id),
            "operation_key": job.operation_key,
            "job_fingerprint": job.fingerprint,
            **extra,
        }

    def test_constructor_requires_explicit_network_opt_in(self):
        with self.assertRaises(NetworkAccessDisabled):
            HTTPBrowserWorkerClient(self.config())

    def test_config_requires_https_and_exact_allowlisted_worker_host(self):
        with self.assertRaises(BrowserWorkerProtocolError):
            self.config(base_url="http://worker.example/api/v1")
        with self.assertRaises(BrowserWorkerProtocolError):
            self.config(allowed_worker_hosts=("different.example",))
        with self.assertRaises(BrowserWorkerProtocolError):
            self.config(base_url="https://name:password@worker.example/api/v1")

    def test_submit_result_and_cancel_use_bearer_and_exact_binding(self):
        job = self.job()
        opener = FakeOpener(
            FakeResponse(self.binding(job, accepted=True)),
            FakeResponse(
                self.binding(
                    job,
                    status=BrowserWorkerJobStatus.SUCCEEDED.value,
                    completed_at=(NOW + timedelta(minutes=1)).isoformat(),
                    items=[{"external_id": "browser-1", "title": "Result"}],
                    reason="",
                )
            ),
            FakeResponse(self.binding(job, cancelled=True)),
        )
        client = HTTPBrowserWorkerClient(self.config(), allow_network=True, opener=opener)
        client.submit(job)
        result = client.result(job.job_id)
        self.assertIsNotNone(result)
        self.assertEqual(result.job_id, job.job_id)
        self.assertEqual(result.operation_key, job.operation_key)
        self.assertEqual(result.job_fingerprint, job.fingerprint)
        client.cancel(job.job_id, "Owner stopped the browser fallback")

        self.assertEqual(len(opener.calls), 3)
        for request, timeout in opener.calls:
            self.assertEqual(timeout, 4)
            self.assertTrue(request.full_url.startswith("https://worker.example/api/v1/jobs"))
            self.assertEqual(request.get_header("Authorization"), "Bearer paired-worker-secret")
            self.assertNotIn("paired-worker-secret", request.full_url)
        submitted = json.loads(opener.calls[0][0].data.decode("utf-8"))
        self.assertEqual(submitted["job_id"], str(job.job_id))
        self.assertEqual(submitted["operation_key"], job.operation_key)
        self.assertEqual(submitted["job_fingerprint"], job.fingerprint)
        self.assertIn("operation_key=", opener.calls[1][0].full_url)
        self.assertIn("job_fingerprint=", opener.calls[1][0].full_url)

    def test_pending_result_still_must_bind_exact_job(self):
        job = self.job()
        opener = FakeOpener(
            FakeResponse(self.binding(job, accepted=True)),
            FakeResponse(self.binding(job, state="PENDING")),
        )
        client = HTTPBrowserWorkerClient(self.config(), allow_network=True, opener=opener)
        client.submit(job)
        self.assertIsNone(client.result(job.job_id))

    def test_mismatched_result_and_job_id_collision_are_rejected(self):
        job = self.job()
        mismatched = self.binding(job, state="PENDING")
        mismatched["operation_key"] = "different-operation"
        opener = FakeOpener(
            FakeResponse(self.binding(job, accepted=True)),
            FakeResponse(mismatched),
        )
        client = HTTPBrowserWorkerClient(self.config(), allow_network=True, opener=opener)
        client.submit(job)
        with self.assertRaises(BrowserWorkerProtocolError):
            client.result(job.job_id)
        collision = replace(job, operation_key="daily:collision")
        with self.assertRaises(BrowserWorkerProtocolError):
            client.submit(collision)
        self.assertEqual(len(opener.calls), 2)

    def test_redirect_or_cross_host_final_url_is_rejected(self):
        job = self.job()
        cross_host = FakeOpener(
            FakeResponse(
                self.binding(job, accepted=True),
                url="https://evil.example/api/v1/jobs",
            )
        )
        client = HTTPBrowserWorkerClient(self.config(), allow_network=True, opener=cross_host)
        with self.assertRaises(BrowserWorkerProtocolError):
            client.submit(job)

        redirect = FakeOpener(FakeResponse(self.binding(job, accepted=True), status=302))
        client = HTTPBrowserWorkerClient(self.config(), allow_network=True, opener=redirect)
        with self.assertRaises(BrowserWorkerProtocolError):
            client.submit(job)

    def test_request_and_response_byte_limits_fail_before_acceptance(self):
        job = self.job()
        request_opener = FakeOpener()
        client = HTTPBrowserWorkerClient(
            self.config(max_request_bytes=10),
            allow_network=True,
            opener=request_opener,
        )
        with self.assertRaises(BrowserWorkerProtocolError):
            client.submit(job)
        self.assertEqual(request_opener.calls, [])

        response_opener = FakeOpener(FakeResponse(self.binding(job, accepted=True)))
        client = HTTPBrowserWorkerClient(
            self.config(max_response_bytes=20),
            allow_network=True,
            opener=response_opener,
        )
        with self.assertRaises(BrowserWorkerProtocolError):
            client.submit(job)

    def test_result_and_cancel_require_a_locally_known_exact_job(self):
        opener = FakeOpener()
        client = HTTPBrowserWorkerClient(self.config(), allow_network=True, opener=opener)
        with self.assertRaises(BrowserWorkerProtocolError):
            client.result(uuid.uuid4())
        with self.assertRaises(BrowserWorkerProtocolError):
            client.cancel(uuid.uuid4(), "unknown")
        self.assertEqual(opener.calls, [])


if __name__ == "__main__":
    unittest.main()
