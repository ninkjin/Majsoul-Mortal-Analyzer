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

    def test_missing_api_key_uses_public_desktop_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "missing.json"
            environment = {
                "MORTAL_PAIPU_CONFIG": str(config_path),
                "MORTAL_PAIPU_API_KEY": "",
                "MORTAL_PAIPU_SERVICE_URL": "https://ninklang.tech",
            }
            with patch.dict(os.environ, environment, clear=False):
                config = remote.load_remote_service_config()

        self.assertEqual(config.api_key, "")

    def test_rejects_invalid_nonempty_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = {
                "MORTAL_PAIPU_CONFIG": str(Path(tmp) / "missing.json"),
                "MORTAL_PAIPU_API_KEY": "not-a-real-key",
                "MORTAL_PAIPU_SERVICE_URL": "https://ninklang.tech",
            }
            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(remote.RemoteServiceError, "格式无效"):
                    remote.load_remote_service_config()


class RemoteServiceFetchTest(unittest.TestCase):
    def test_public_desktop_create_poll_and_download_without_api_key(self):
        result = {
            "ver": "2.3",
            "name": ["a", "b", "c", "d"],
            "rule": {"disp": "四人南"},
            "log": [],
            "_target_actor": 1,
        }
        responses = [
            {
                "request_id": "request-public-1",
                "request_token": "rq_" + "t" * 32,
                "status": "queued",
                "poll_after_ms": 1,
            },
            {"request_id": "request-public-1", "status": "ready", "poll_after_ms": 1},
            result,
        ]
        calls = []

        def fake_request(url, api_key, **kwargs):
            calls.append((url, api_key, kwargs))
            return responses.pop(0)

        config = remote.RemoteServiceConfig("https://ninklang.tech", "")
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "record.json"
            with (
                patch.object(remote, "load_remote_service_config", return_value=config),
                patch.object(remote, "_request_json", side_effect=fake_request),
            ):
                remote.fetch_remote_tenhou(
                    "https://game.maj-soul.com/1/?paipu=example",
                    out_path,
                    sleep=lambda _seconds: None,
                )
            saved = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["_target_actor"], 1)
        self.assertTrue(calls[0][0].endswith("/api/v1/desktop/requests"))
        self.assertEqual(calls[0][1], "")
        self.assertEqual(calls[1][2]["authorization_scheme"], "Request")
        self.assertEqual(calls[2][2]["authorization_scheme"], "Request")

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

    def test_result_rejects_boolean_target_actor(self):
        with self.assertRaisesRegex(remote.RemoteServiceError, "目标玩家位置无效"):
            remote._validate_result({
                "ver": "2.3",
                "name": ["a", "b", "c", "d"],
                "log": [],
                "_target_actor": True,
            })

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

    def test_service_error_text_is_bounded_and_single_line(self):
        message = "first line\n" + "x" * 1000
        rendered = remote._safe_error_message(400, {"error": {"code": "INVALID", "message": message}})

        self.assertNotIn("\n", rendered)
        self.assertLess(len(rendered), 400)
        self.assertTrue(rendered.endswith("..."))

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
        with patch.object(remote, "_open_url", side_effect=error):
            with self.assertRaises(remote.RemoteServiceError) as raised:
                remote._request_json("https://ninklang.tech/api/v1/client/requests", api_key)

        self.assertIn("API Key", str(raised.exception))
        self.assertNotIn(api_key, str(raised.exception))

    def test_service_error_echo_cannot_expose_api_key(self):
        api_key = "pk_" + "secret" * 8
        error = urllib.error.HTTPError(
            "https://ninklang.tech/api/v1/client/requests",
            403,
            "Forbidden",
            {},
            BytesIO(json.dumps({"detail": f"rejected token {api_key}"}).encode("utf-8")),
        )
        with patch.object(remote, "_open_url", side_effect=error):
            with self.assertRaises(remote.RemoteServiceError) as raised:
                remote._request_json("https://ninklang.tech/api/v1/client/requests", api_key)

        self.assertNotIn(api_key, str(raised.exception))
        self.assertIn("***", str(raised.exception))

    def test_http_500_is_treated_as_a_retryable_service_failure(self):
        error = urllib.error.HTTPError(
            "https://ninklang.tech/api/v1/requests/request-1",
            500,
            "Internal Server Error",
            {},
            BytesIO(b"Internal Server Error"),
        )
        with patch.object(remote, "_open_url", side_effect=error):
            with self.assertRaises(remote.RemoteServiceConnectionError):
                remote._request_json(
                    "https://ninklang.tech/api/v1/requests/request-1",
                    "pk_" + "x" * 32,
                )

    def test_http_rate_limit_is_retryable_and_honors_retry_after(self):
        error = urllib.error.HTTPError(
            "https://ninklang.tech/api/v1/client/requests",
            429,
            "Too Many Requests",
            {"Retry-After": "3"},
            BytesIO(b'{"detail":"rate limited"}'),
        )
        with patch.object(remote, "_open_url", side_effect=error):
            with self.assertRaises(remote.RemoteServiceConnectionError) as raised:
                remote._request_json(
                    "https://ninklang.tech/api/v1/client/requests",
                    "pk_" + "x" * 32,
                )

        self.assertEqual(raised.exception.retry_after, 3)

    def test_redirects_are_not_followed_with_authorization_header(self):
        request = urllib.request.Request(
            "https://ninklang.tech/api/v1/client/requests",
            headers={"Authorization": "Bearer pk_secret"},
        )
        redirected = remote.NoRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.example/collect",
        )

        self.assertIsNone(redirected)

    def test_retry_uses_service_retry_after_delay(self):
        delays = []
        responses = [
            remote.RemoteServiceConnectionError("rate limited", retry_after=4),
            {"status": "ready"},
        ]
        with patch.object(remote, "_request_json", side_effect=responses):
            result = remote._request_json_with_retries(
                "https://ninklang.tech/api/v1/requests/request-1",
                "pk_" + "x" * 32,
                sleep=delays.append,
                clock=lambda: 0,
                deadline=10,
            )

        self.assertEqual(result, {"status": "ready"})
        self.assertEqual(delays, [4])

    def test_deadline_caps_each_network_request_timeout(self):
        captured = []

        def fake_request(url, api_key, **kwargs):
            captured.append(kwargs["timeout"])
            return {"status": "ready"}

        with patch.object(remote, "_request_json", side_effect=fake_request):
            remote._request_json_with_retries(
                "https://ninklang.tech/api/v1/requests/request-1",
                "pk_" + "x" * 32,
                timeout=30,
                clock=lambda: 8,
                deadline=10,
            )

        self.assertEqual(captured, [2])

    def test_deadline_prevents_retry_sleep_from_overshooting(self):
        delays = []
        with patch.object(
            remote,
            "_request_json",
            side_effect=remote.RemoteServiceConnectionError("temporary timeout"),
        ):
            with self.assertRaisesRegex(remote.RemoteServiceError, "等待远程牌谱服务超时"):
                remote._request_json_with_retries(
                    "https://ninklang.tech/api/v1/requests/request-1",
                    "pk_" + "x" * 32,
                    sleep=delays.append,
                    clock=lambda: 9,
                    deadline=10,
                )

        self.assertEqual(delays, [])

    def test_poll_sleep_stops_exactly_at_overall_deadline(self):
        config = remote.RemoteServiceConfig("https://ninklang.tech", "pk_" + "x" * 32)
        now = [0.0]
        delays = []

        def clock():
            return now[0]

        def sleep(seconds):
            delays.append(seconds)
            now[0] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(remote, "load_remote_service_config", return_value=config),
                patch.object(
                    remote,
                    "_request_json",
                    return_value={"request_id": "request-1", "status": "queued", "poll_after_ms": 5000},
                ) as request_json,
            ):
                with self.assertRaisesRegex(remote.RemoteServiceError, "等待远程牌谱服务超时"):
                    remote.fetch_remote_tenhou(
                        "https://game.maj-soul.com/1/?paipu=example",
                        Path(tmp) / "record.json",
                        max_wait_seconds=2,
                        sleep=sleep,
                        clock=clock,
                    )

        self.assertEqual(delays, [2])
        self.assertEqual(request_json.call_count, 1)


if __name__ == "__main__":
    unittest.main()
