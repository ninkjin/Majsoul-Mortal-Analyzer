import json
import os
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote_service_fetcher as remote


class RemoteServiceConfigTest(unittest.TestCase):
    def test_loads_local_config_without_exposing_it_to_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "local.json"
            config_path.write_text(
                json.dumps({"service_url": "https://ninklang.tech", "api_key": "pk_" + "a" * 32}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MORTAL_PAIPU_CONFIG": str(config_path)}, clear=False):
                config = remote.load_remote_service_config()

        self.assertEqual(config.service_url, "https://ninklang.tech")
        self.assertTrue(config.api_key.startswith("pk_"))

    def test_environment_overrides_file_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "local.json"
            config_path.write_text(
                json.dumps({"service_url": "https://old.example", "api_key": "pk_" + "a" * 32}),
                encoding="utf-8",
            )
            environment = {
                "MORTAL_PAIPU_CONFIG": str(config_path),
                "MORTAL_PAIPU_SERVICE_URL": "https://ninklang.tech/",
                "MORTAL_PAIPU_API_KEY": "pk_" + "b" * 32,
            }
            with patch.dict(os.environ, environment, clear=False):
                config = remote.load_remote_service_config()

        self.assertEqual(config.service_url, "https://ninklang.tech")
        self.assertEqual(config.api_key, "pk_" + "b" * 32)

    def test_rejects_missing_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "missing.json"
            environment = {
                "MORTAL_PAIPU_CONFIG": str(config_path),
                "MORTAL_PAIPU_API_KEY": "",
                "MORTAL_PAIPU_SERVICE_URL": "https://ninklang.tech",
            }
            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(remote.RemoteServiceError, "API Key"):
                    remote.load_remote_service_config()


class RemoteServiceFetchTest(unittest.TestCase):
    def test_create_poll_download_and_write_tenhou_json(self):
        result = {
            "ver": "2.3",
            "name": ["a", "b", "c", "d"],
            "rule": {"disp": "四人南"},
            "log": [],
            "_target_actor": 2,
        }
        responses = [
            {"request_id": "request-1", "status": "queued", "poll_after_ms": 1},
            {"request_id": "request-1", "status": "fetching", "poll_after_ms": 1, "error": None},
            {"request_id": "request-1", "status": "ready", "poll_after_ms": 1, "error": None},
            result,
        ]
        calls = []

        def fake_request(url, api_key, **kwargs):
            calls.append((url, api_key, kwargs))
            return responses.pop(0)

        config = remote.RemoteServiceConfig("https://ninklang.tech", "pk_" + "x" * 32)
        statuses = []
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "record.json"
            with (
                patch.object(remote, "load_remote_service_config", return_value=config),
                patch.object(remote, "_request_json", side_effect=fake_request),
            ):
                remote.fetch_remote_tenhou(
                    "https://game.maj-soul.com/1/?paipu=example",
                    out_path,
                    status_callback=statuses.append,
                    sleep=lambda _seconds: None,
                )
            saved = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(statuses, ["queued", "fetching", "ready"])
        self.assertEqual(saved["_target_actor"], 2)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][2]["method"], "POST")
        self.assertIn("Idempotency-Key", calls[0][2]["extra_headers"])
        self.assertTrue(calls[-1][0].endswith("/request-1/result"))

    def test_failed_status_surfaces_safe_service_error(self):
        responses = [
            {"request_id": "request-1", "status": "queued", "poll_after_ms": 1},
            {
                "request_id": "request-1",
                "status": "failed",
                "error": {"code": "ACCOUNT_REAUTH_REQUIRED", "message": "对应平台暂时需要重新认证。"},
            },
        ]
        config = remote.RemoteServiceConfig("https://ninklang.tech", "pk_" + "x" * 32)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(remote, "load_remote_service_config", return_value=config),
                patch.object(remote, "_request_json", side_effect=responses),
            ):
                with self.assertRaisesRegex(remote.RemoteServiceError, "ACCOUNT_REAUTH_REQUIRED"):
                    remote.fetch_remote_tenhou("https://example.invalid/?paipu=x", Path(tmp) / "x.json", sleep=lambda _: None)

    def test_temporary_network_timeout_is_retried_without_losing_the_request(self):
        result = {"ver": "2.3", "name": ["a", "b", "c", "d"], "log": [], "_target_actor": 1}
        responses = [
            remote.RemoteServiceConnectionError("temporary timeout"),
            {"request_id": "request-1", "status": "ready", "poll_after_ms": 1},
            result,
        ]
        config = remote.RemoteServiceConfig("https://ninklang.tech", "pk_" + "x" * 32)
        delays = []
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "record.json"
            with (
                patch.object(remote, "load_remote_service_config", return_value=config),
                patch.object(remote, "_request_json", side_effect=responses) as request_json,
            ):
                remote.fetch_remote_tenhou(
                    "https://game.maj-soul.com/1/?paipu=example",
                    out_path,
                    sleep=delays.append,
                )

        self.assertEqual(delays, [1])
        self.assertEqual(request_json.call_count, 3)

    def test_http_unauthorized_message_does_not_contain_api_key(self):
        api_key = "pk_" + "secret" * 8
        error = urllib.error.HTTPError(
            "https://ninklang.tech/api/v1/client/requests",
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"detail":"Invalid API key"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(remote.RemoteServiceError) as raised:
                remote._request_json("https://ninklang.tech/api/v1/client/requests", api_key)

        self.assertIn("API Key", str(raised.exception))
        self.assertNotIn(api_key, str(raised.exception))

    def test_http_500_is_treated_as_a_retryable_service_failure(self):
        error = urllib.error.HTTPError(
            "https://ninklang.tech/api/v1/requests/request-1",
            500,
            "Internal Server Error",
            {},
            BytesIO(b"Internal Server Error"),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(remote.RemoteServiceConnectionError):
                remote._request_json(
                    "https://ninklang.tech/api/v1/requests/request-1",
                    "pk_" + "x" * 32,
                )


if __name__ == "__main__":
    unittest.main()
