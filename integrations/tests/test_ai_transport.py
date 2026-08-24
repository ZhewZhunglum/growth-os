from __future__ import annotations

import json
import unittest

from integrations.ai.transport import UrllibHTTPTransport
from integrations.errors import NetworkAccessDisabled, ProviderResponseError


class FakeResponse:
    def __init__(self, payload, *, status=200, url=None, headers=None):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.status = status
        self.url = url
        self.headers = headers or {"x-request-id": "fake-ai-1"}

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


class UrllibHTTPTransportTests(unittest.TestCase):
    def test_constructor_requires_explicit_network_opt_in(self):
        with self.assertRaises(NetworkAccessDisabled):
            UrllibHTTPTransport()

    def test_default_allows_only_deepseek_https_without_url_credentials(self):
        opener = FakeOpener(FakeResponse({"ok": True}))
        transport = UrllibHTTPTransport(allow_network=True, opener=opener)
        response = transport.post_json(
            url="https://api.deepseek.com/chat/completions",
            headers={"Authorization": "Bearer fake"},
            payload={"model": "deepseek-v4-flash"},
            timeout_seconds=5,
        )
        self.assertEqual(response.payload, {"ok": True})
        self.assertEqual(response.request_id, "fake-ai-1")
        request, timeout = opener.calls[0]
        self.assertEqual(timeout, 5)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake")

        for unsafe in (
            "http://api.deepseek.com/chat/completions",
            "https://other.example/chat/completions",
            "https://name:password@api.deepseek.com/chat/completions",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ProviderResponseError):
                transport.post_json(url=unsafe, headers={}, payload={}, timeout_seconds=5)
        self.assertEqual(len(opener.calls), 1)

    def test_custom_provider_requires_explicit_allowed_host(self):
        opener = FakeOpener(FakeResponse({"ok": True}))
        transport = UrllibHTTPTransport(
            allow_network=True,
            allowed_hosts=("ai.internal.example",),
            opener=opener,
        )
        response = transport.post_json(
            url="https://ai.internal.example/v1/generate",
            headers={},
            payload={"prompt": "offline fixture"},
            timeout_seconds=2,
        )
        self.assertEqual(response.status_code, 200)

    def test_redirect_and_cross_host_final_url_are_rejected(self):
        cross_host = FakeOpener(
            FakeResponse({"ok": True}, url="https://evil.example/stolen")
        )
        transport = UrllibHTTPTransport(allow_network=True, opener=cross_host)
        with self.assertRaises(ProviderResponseError):
            transport.post_json(
                url="https://api.deepseek.com/chat/completions",
                headers={},
                payload={},
                timeout_seconds=2,
            )

        redirect = FakeOpener(FakeResponse({"ok": True}, status=302))
        transport = UrllibHTTPTransport(allow_network=True, opener=redirect)
        with self.assertRaises(ProviderResponseError):
            transport.post_json(
                url="https://api.deepseek.com/chat/completions",
                headers={},
                payload={},
                timeout_seconds=2,
            )

    def test_request_response_and_timeout_limits_fail_closed(self):
        request_opener = FakeOpener()
        transport = UrllibHTTPTransport(
            allow_network=True,
            max_request_bytes=5,
            opener=request_opener,
        )
        with self.assertRaises(ProviderResponseError):
            transport.post_json(
                url="https://api.deepseek.com/chat/completions",
                headers={},
                payload={"too": "large"},
                timeout_seconds=2,
            )
        self.assertEqual(request_opener.calls, [])

        response_opener = FakeOpener(FakeResponse({"long": "response"}))
        transport = UrllibHTTPTransport(
            allow_network=True,
            max_response_bytes=10,
            opener=response_opener,
        )
        with self.assertRaises(ProviderResponseError):
            transport.post_json(
                url="https://api.deepseek.com/chat/completions",
                headers={},
                payload={},
                timeout_seconds=2,
            )

        transport = UrllibHTTPTransport(allow_network=True, opener=FakeOpener())
        for timeout in (0, 121):
            with self.subTest(timeout=timeout), self.assertRaises(ProviderResponseError):
                transport.post_json(
                    url="https://api.deepseek.com/chat/completions",
                    headers={},
                    payload={},
                    timeout_seconds=timeout,
                )

    def test_one_failed_response_is_not_retried(self):
        opener = FakeOpener(FakeResponse({"error": "busy"}, status=503))
        transport = UrllibHTTPTransport(allow_network=True, opener=opener)
        with self.assertRaises(ProviderResponseError):
            transport.post_json(
                url="https://api.deepseek.com/chat/completions",
                headers={},
                payload={},
                timeout_seconds=2,
            )
        self.assertEqual(len(opener.calls), 1)

    def test_invalid_json_or_non_object_response_is_rejected(self):
        for payload in (b"not-json", ["not", "an", "object"]):
            with self.subTest(payload=payload):
                opener = FakeOpener(FakeResponse(payload))
                transport = UrllibHTTPTransport(allow_network=True, opener=opener)
                with self.assertRaises(ProviderResponseError):
                    transport.post_json(
                        url="https://api.deepseek.com/chat/completions",
                        headers={},
                        payload={},
                        timeout_seconds=2,
                    )

    def test_caller_cannot_override_host_or_content_length(self):
        transport = UrllibHTTPTransport(allow_network=True, opener=FakeOpener())
        for header in ("Host", "Content-Length"):
            with self.subTest(header=header), self.assertRaises(ProviderResponseError):
                transport.post_json(
                    url="https://api.deepseek.com/chat/completions",
                    headers={header: "evil.example"},
                    payload={},
                    timeout_seconds=2,
                )


if __name__ == "__main__":
    unittest.main()
