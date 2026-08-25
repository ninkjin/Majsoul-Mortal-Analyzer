import http.client
import json
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class ServerHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("private", encoding="utf-8")
        (self.root / "paipu-service.local.json").write_text('{"api_key":"private"}', encoding="utf-8")
        (self.root / "mortal-output-viewer.html").write_text("viewer", encoding="utf-8")
        asset_dir = self.root / "log-viewer" / "files" / "images"
        asset_dir.mkdir(parents=True)
        (asset_dir / "blank.png").write_bytes(b"image")
        (self.root / "mj_model").mkdir()
        (self.root / "mj_model" / "mortal.pth").write_bytes(b"model")
        self.root_patch = patch.object(server, "ROOT", self.root)
        self.root_patch.start()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.root_patch.stop()
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def test_private_files_are_blocked_case_insensitively(self):
        for path in ("/paipu-service.local.json", "/PAIPU-SERVICE.LOCAL.JSON", "/.git/config", "/.GIT/config"):
            with self.subTest(path=path):
                status, _ = self.request("GET", path)
                self.assertEqual(status, 403)

    def test_static_allowlist_serves_viewer_but_not_models(self):
        status, payload = self.request("GET", "/mortal-output-viewer.html")
        self.assertEqual((status, payload), (200, b"viewer"))

        status, _ = self.request("GET", "/mj_model/mortal.pth")
        self.assertEqual(status, 404)

    def test_static_assets_are_cached_but_viewer_html_is_not(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=2)
        connection.request("GET", "/log-viewer/files/images/blank.png")
        asset_response = connection.getresponse()
        asset_response.read()
        self.assertEqual(asset_response.version, 11)
        self.assertEqual(asset_response.getheader("Cache-Control"), "public, max-age=86400")
        connection.request("GET", "/mortal-output-viewer.html")
        viewer_response = connection.getresponse()
        viewer_response.read()
        self.assertEqual(viewer_response.getheader("Cache-Control"), "no-store")
        connection.close()

    def test_invalid_history_limit_returns_json_error(self):
        status, payload = self.request("GET", "/api/history?limit=bad")
        self.assertEqual(status, 400)
        self.assertIn("limit", json.loads(payload)["error"])

    def test_oversized_request_body_is_rejected(self):
        status, payload = self.request(
            "POST",
            "/api/analyze",
            body=b"{}",
            headers={
                "Content-Length": str(server.MAX_REQUEST_BODY_BYTES + 1),
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(status, 413)
        self.assertIn("请求体过大", json.loads(payload)["error"])

    def test_untrusted_host_is_rejected(self):
        status, _ = self.request("GET", "/api/models", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)

    def test_cross_origin_post_is_rejected(self):
        status, _ = self.request(
            "POST",
            "/api/analyze",
            body=b"{}",
            headers={"Content-Type": "application/json", "Origin": "http://attacker.example"},
        )
        self.assertEqual(status, 403)

    def test_json_endpoints_reject_browser_simple_content_types(self):
        status, payload = self.request(
            "POST",
            "/api/analyze",
            body=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(payload)["error"])

    def test_request_log_suppresses_successful_get_noise_only(self):
        self.assertEqual(server.Handler.timeout, 10)
        handler = object.__new__(server.Handler)
        output = StringIO()
        with patch("sys.stderr", output):
            handler.log_message("Request timed out: %r", TimeoutError("idle connection"))
            handler.log_message('"%s" %s %s', "GET /api/status?job_id=x HTTP/1.1", "200", "-")
            self.assertEqual(output.getvalue(), "")
            handler.log_message('"%s" %s %s', "GET /api/status?job_id=x HTTP/1.1", "404", "-")
            handler.log_message('"%s" %s %s', "POST /api/analyze HTTP/1.1", "200", "-")

        self.assertIn("404", output.getvalue())
        self.assertIn("POST /api/analyze", output.getvalue())


if __name__ == "__main__":
    unittest.main()
