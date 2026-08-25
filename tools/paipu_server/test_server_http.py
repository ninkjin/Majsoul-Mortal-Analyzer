import http.client
import json
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()
