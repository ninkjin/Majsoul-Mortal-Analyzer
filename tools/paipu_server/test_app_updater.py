import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_updater as updater
import update_worker as worker
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class SignedUpdateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "app"
        self.root.mkdir()
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes_raw()).decode()
        self.current = {"version": "2.2.0", "runtime_abi": "win-amd64-cp312", "updater_protocol": 1}
        updater.atomic_json(self.root / "app-version.json", self.current)
        updater.atomic_json(self.root / "update-trust.json", {"schema": 1, "public_key": self.public})
        self.files = {name: b"# test application file\n" for name in updater.REQUIRED_FILES}
        self.files["app-version.json"] = json.dumps({**self.current, "version": "2.2.1"}).encode()
        self.files["mortal/sample.py"] = b"new_code = True\n"
        self.doc = {
            "schema": 1, "version": "2.2.1", "updater_protocol": 1, "runtime_abi": self.current["runtime_abi"],
            "created": int(time.time()) - 10, "expires": int(time.time()) + 86400,
            "notes": "Test update", "url": f"https://github.com/{updater.REPOSITORY}/releases/download/v2.2.1/mortal-update-2.2.1.zip",
            "size": 1, "sha256": "a" * 64, "files": {}, "delete": [],
        }
        self.refresh_files()

    def tearDown(self):
        self.temp.cleanup()

    def refresh_files(self):
        self.doc["files"] = {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in self.files.items()}

    def signed(self, doc=None):
        raw = json.dumps(doc or self.doc).encode()
        return raw, base64.b64encode(self.key.sign(raw))

    def verify(self, doc=None):
        raw, signature = self.signed(doc)
        return updater.verify_manifest(raw, signature, self.public, current=self.current)

    def stage(self, token="a" * 32):
        base = self.root / ".updates" / token
        (base / "app").mkdir(parents=True)
        for name, data in self.files.items():
            path = base / "app" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        raw, signature = self.signed()
        (base / "manifest.json").write_bytes(raw)
        (base / "manifest.sig").write_bytes(signature)
        updater.atomic_json(base / "plan.json", {"current": self.current, "public_key": self.public,
                            "parent_pid": os.getpid(), "host": "127.0.0.1", "port": 18765, "health_token": "test-probe"})
        updater.atomic_json(base / "prepared.json", {"version": "2.2.1", "has_dependencies": False})
        updater.atomic_json(self.root / ".updates/pending.json", {"transaction": base.name})
        return base

    def test_signature_matches_and_tampering_is_rejected(self):
        self.assertEqual(self.verify()["version"], "2.2.1")
        raw, signature = self.signed()
        for bad_raw, bad_sig in ((raw + b" ", signature), (raw, base64.b64encode(b"0" * 64))):
            with self.assertRaisesRegex(updater.UpdateError, "签名"):
                updater.verify_manifest(bad_raw, bad_sig, self.public, current=self.current)

    def test_versions_are_numeric_and_downgrades_are_forbidden(self):
        self.assertGreater(updater.version_tuple("2.10.0"), updater.version_tuple("2.9.0"))
        for version in ("2.2.0", "2.1.9", "2.2.1-beta", "02.2.1"):
            with self.subTest(version=version), self.assertRaises(updater.UpdateError):
                self.verify({**self.doc, "version": version})

    def test_expired_future_or_incompatible_manifest_is_rejected(self):
        cases = [{"expires": 1}, {"created": int(time.time()) + 90000},
                 {"runtime_abi": "other"}, {"updater_protocol": 999}, {"size": updater.MAX_PACKAGE + 1}]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(updater.UpdateError):
                self.verify({**self.doc, **fields})

    def test_personal_files_runtime_models_and_trust_cannot_be_updated(self):
        for name in ("paipu-service.local.json", "runtime/python.exe", "mj_model/mortal.pth",
                     "viewer-data/log.json", "tmp/paipu_jobs/a/log.json", "update-trust.json", ".updates/plan.json"):
            with self.subTest(name=name), self.assertRaises(updater.UpdateError):
                doc = copy.deepcopy(self.doc)
                doc["files"][name] = {"size": 1, "sha256": "a" * 64}
                self.verify(doc)

    def test_windows_path_attacks_are_rejected(self):
        for name in ("../secret", "mortal/../server.py", "C:/file.py", "mortal\\evil.py", "/a.py",
                     "mortal/a.py:stream", "mortal/a. /b.py", "mortal/con.py", "mortal/a//b.py", "mortal/./b.py"):
            with self.subTest(name=name), self.assertRaises(updater.UpdateError):
                updater.safe_name(name)

    def test_case_collisions_and_removing_required_files_are_rejected(self):
        doc = copy.deepcopy(self.doc)
        doc["files"]["mortal/SAMPLE.py"] = doc["files"]["mortal/sample.py"]
        with self.assertRaises(updater.UpdateError):
            self.verify(doc)
        with self.assertRaises(updater.UpdateError):
            self.verify({**self.doc, "delete": ["tools/paipu_server/server.py"]})

    def test_package_url_is_pinned_to_expected_repository_and_version(self):
        with self.assertRaises(updater.UpdateError):
            self.verify({**self.doc, "url": "https://github.com/attacker/repo/releases/download/v2.2.1/p.zip"})
        for url in ("http://github.com/x", "https://127.0.0.1/x", "https://github.com.evil.test/x", "https://user@github.com/x"):
            with self.subTest(url=url), self.assertRaises(updater.UpdateError):
                updater.checked_download_url(url)

    def package(self, override=None, extra=None, symlink=False):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for name, contents in {**self.files, **(override or {})}.items():
                if symlink and name == "mortal/sample.py":
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, contents)
                else:
                    archive.writestr(name, contents)
            if extra:
                archive.writestr(extra, b"evil")
        data.seek(0)
        return data

    def test_archive_is_exactly_verified(self):
        updater.unpack_verified(self.package(), self.root / "good", self.verify())
        self.assertEqual((self.root / "good/mortal/sample.py").read_bytes(), self.files["mortal/sample.py"])
        for kwargs in ({"override": {"mortal/sample.py": b"evil"}}, {"extra": "../escape"}, {"symlink": True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as temp, self.assertRaises(updater.UpdateError):
                updater.unpack_verified(self.package(**kwargs), Path(temp) / "bad", self.doc)

    def test_apply_and_rollback_preserve_history_model_and_config(self):
        base = self.stage()
        old = self.root / "mortal/sample.py"
        old.parent.mkdir()
        old.write_text("old_code = True\n")
        personal = {"paipu-service.local.json": b"private config", "viewer-data/log.json": b"history",
                    "mj_model/model.pth": b"model", "tmp/paipu_jobs/a/log.json": b"record"}
        for name, data in personal.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        worker.apply_files(self.root, base, self.doc)
        self.assertEqual(updater.app_info(self.root)["version"], "2.2.1")
        worker.rollback(self.root, base)
        self.assertEqual(updater.app_info(self.root)["version"], "2.2.0")
        self.assertEqual(old.read_text(), "old_code = True\n")
        self.assertFalse((self.root / "tools/paipu_server/server.py").exists())
        for name, data in personal.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        worker.rollback(self.root, base)  # idempotent

    def test_partial_replace_failure_is_recoverable_on_next_start(self):
        base = self.stage()
        actual = worker.durable_copy

        def failing(source, target):
            if Path(target) == self.root / "mortal/sample.py":
                raise OSError("simulated locked file")
            return actual(source, target)

        with patch.object(worker, "durable_copy", side_effect=failing), self.assertRaises(OSError):
            worker.apply_files(self.root, base, self.doc)
        worker.recover_pending(self.root)
        self.assertEqual(updater.app_info(self.root), self.current)
        self.assertEqual(updater.read_json(self.root / ".updates/status.json")["state"], "rolled_back")
        self.assertFalse((self.root / ".updates/pending.json").exists())

    def test_corrupt_backup_stops_recovery_without_deleting_evidence(self):
        base = self.stage()
        worker.apply_files(self.root, base, self.doc)
        (base / "backup/0").write_bytes(b"corrupt")
        with self.assertRaises(updater.UpdateError):
            worker.recover_pending(self.root)
        self.assertTrue((self.root / ".updates/pending.json").exists())

    def test_new_server_health_failure_rolls_back_and_restarts_old_version(self):
        base = self.stage()
        launches = []

        def launch(root, plan):
            launches.append(updater.app_info(root)["version"])
            return Mock(poll=lambda: 0)

        ok = worker.run_update(self.root, base, launch=launch, health=lambda *_: False, wait_parent=lambda *_: None, deadline=0)
        self.assertFalse(ok)
        self.assertEqual(launches, ["2.2.1", "2.2.0"])
        self.assertEqual(updater.app_info(self.root), self.current)

    def test_healthy_update_commits_and_is_not_rolled_back_on_next_start(self):
        base = self.stage()
        ok = worker.run_update(self.root, base, launch=lambda *_: Mock(poll=lambda: 0),
                               health=lambda *_: True, wait_parent=lambda *_: None, deadline=1)
        self.assertTrue(ok)
        worker.recover_pending(self.root)
        self.assertEqual(updater.app_info(self.root)["version"], "2.2.1")
        self.assertEqual(updater.read_json(base / "journal.json")["phase"], "committed")

    def test_dependencies_overlay_is_transactional(self):
        base = self.stage()
        (base / "dependencies").mkdir()
        updater.atomic_json(base / "prepared.json", {"has_dependencies": True})
        worker.apply_files(self.root, base, self.doc)
        self.assertEqual(updater.dependency_path(self.root), base / "dependencies")
        worker.rollback(self.root, base)
        self.assertIsNone(updater.dependency_path(self.root))

    def test_download_failure_keeps_current_application(self):
        manager = updater.UpdateManager(self.root, shutdown=Mock())
        manager.candidate = (*self.signed(), self.doc)
        manager.installing = True
        with patch.object(updater, "download", side_effect=updater.UpdateError("offline")):
            manager._prepare()
        self.assertEqual(updater.app_info(self.root), self.current)
        self.assertFalse(manager.installing)
        self.assertEqual(manager.snapshot()["state"], "error")
        manager.shutdown.assert_not_called()

    def test_developer_checkout_cannot_be_overwritten(self):
        (self.root / ".git").mkdir()
        manager = updater.UpdateManager(self.root, shutdown=Mock())
        manager.candidate = (*self.signed(), self.doc)
        with self.assertRaisesRegex(updater.UpdateError, "源码目录"):
            manager.install("2.2.1")

    def test_offline_check_does_not_claim_latest_or_send_credentials(self):
        manager = updater.UpdateManager(self.root)
        with patch.object(updater, "download", side_effect=updater.UpdateError("offline")) as network:
            manager._check()
        self.assertEqual(manager.snapshot()["state"], "error")
        self.assertIsNone(manager.candidate)
        self.assertEqual(network.call_args.args, (updater.RELEASE_API,))

    def test_new_release_without_signed_assets_is_not_installable(self):
        manager = updater.UpdateManager(self.root)
        with patch.object(updater, "download", return_value=json.dumps({"tag_name": "v2.2.1", "assets": []}).encode()):
            manager._check()
        self.assertEqual(manager.snapshot()["state"], "error")
        self.assertIsNone(manager.candidate)

    def test_valid_release_becomes_available(self):
        manager = updater.UpdateManager(self.root)
        raw, signature = self.signed()
        prefix = f"https://github.com/{updater.REPOSITORY}/releases/download/v2.2.1/"
        release = {"tag_name": "v2.2.1", "assets": [{"name": name, "browser_download_url": prefix + name}
                                                   for name in ("mortal-update.json", "mortal-update.sig")]}
        with patch.object(updater, "download", side_effect=[json.dumps(release).encode(), raw, signature]):
            manager._check()
        self.assertEqual(manager.snapshot()["state"], "available")
        self.assertEqual(manager.snapshot()["available_version"], "2.2.1")

    def test_changed_stage_is_rejected_before_replacement(self):
        base = self.stage()
        (base / "app/mortal/sample.py").write_text("tampered")
        with self.assertRaises(updater.UpdateError):
            worker.verify_stage(self.root, base)

    def test_interrupted_download_does_not_permanently_block_analysis(self):
        updater.atomic_json(self.root / ".updates/status.json", {"state": "downloading"})
        manager = updater.UpdateManager(self.root)
        self.assertFalse(manager.snapshot()["busy"])
        self.assertEqual(manager.snapshot()["state"], "error")

    def fixture_wheel(self, version):
        name = "mortal_update_fixture"
        dist = f"{name}-{version}.dist-info"
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as package:
            package.writestr(name + ".py", f"version = '{version}'\n")
            package.writestr(dist + "/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
            package.writestr(dist + "/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            package.writestr(dist + "/RECORD", "")
        return f"update-wheels/{name}-{version}-py3-none-any.whl", data.getvalue()

    def test_offline_dependencies_upgrade_in_fresh_overlay_without_touching_old(self):
        name, data = self.fixture_wheel("1.0.0")
        self.files[name] = data
        self.refresh_files()
        first = self.stage()
        worker.prepare(self.root, first)
        updater.atomic_json(self.root / ".updates/active-deps.json", {"transaction": first.name})
        del self.files[name]
        name, data = self.fixture_wheel("2.0.0")
        self.files[name] = data
        self.refresh_files()
        second = self.stage("b" * 32)
        worker.prepare(self.root, second)
        for base, expected in ((first, "1.0.0"), (second, "2.0.0")):
            command = [sys.executable, "-B", "-c", "import mortal_update_fixture as f; import importlib.metadata as m; print(f.version, m.version('mortal-update-fixture'))"]
            result = subprocess.run(command, capture_output=True, text=True, check=True,
                                    env=updater.update_environment(self.root, base / "dependencies"), timeout=20)
            self.assertEqual(result.stdout.strip(), expected + " " + expected)
        self.assertFalse((second / "dependencies/mortal_update_fixture-1.0.0.dist-info").exists())

    def test_only_one_instance_can_hold_installation_lock(self):
        with worker.install_lock(self.root):
            with self.assertRaises((updater.UpdateError, OSError)):
                with worker.install_lock(self.root):
                    self.fail("second updater acquired lock")

    def test_download_enforces_total_size_and_digest(self):
        class Response(io.BytesIO):
            url = "https://github.com/ninkjin/Majsoul-Mortal-Analyzer/releases/download/v2.2.1/package.zip"

        url = Response.url
        for kwargs in ({"limit": 2}, {"limit": 50, "expected_size": 1}, {"limit": 50, "expected_hash": "0" * 64}):
            opener = Mock(open=Mock(return_value=Response(b"payload")))
            with patch.object(updater.urllib.request, "build_opener", return_value=opener), self.assertRaises(updater.UpdateError):
                updater.download(url, **kwargs)
        opener = Mock(open=Mock(return_value=Response(b"payload")))
        with patch.object(updater.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(updater.download(url, limit=50, expected_hash=hashlib.sha256(b"payload").hexdigest()), b"payload")

    def test_release_api_requests_json_but_package_requests_binary(self):
        class Response(io.BytesIO):
            def __init__(self, url):
                super().__init__(b"{}")
                self.url = url

        asset = f"https://github.com/{updater.REPOSITORY}/releases/download/v2.2.1/mortal-update-2.2.1.zip"
        for url, expected in ((updater.RELEASE_API, "application/vnd.github+json"), (asset, "application/octet-stream")):
            with self.subTest(url=url):
                opener = Mock(open=Mock(return_value=Response(url)))
                with patch.object(updater.urllib.request, "build_opener", return_value=opener):
                    updater.download(url, limit=100)
                req = opener.open.call_args.args[0]
                self.assertEqual(req.get_header("Accept"), expected)
                self.assertIsNone(req.get_header("Authorization"))

    def test_http_failure_is_not_misreported_as_network_disconnection(self):
        for code, headers, expected in ((415, {}, "请求格式"), (404, {}, "发布状态"),
                                         (403, {"X-RateLimit-Remaining": "0"}, "次数"),
                                         (429, {}, "次数"), (503, {}, "暂时异常"), (403, {}, "被拒绝")):
            error = updater.urllib.error.HTTPError(updater.RELEASE_API, code, "private-response", headers,
                                                   io.BytesIO(b"private response body"))
            opener = Mock(open=Mock(side_effect=error))
            with self.subTest(code=code), patch.object(updater.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(updater.UpdateError) as caught:
                    updater.download(updater.RELEASE_API, limit=100)
                self.assertIn(str(code), str(caught.exception))
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("无法连接", str(caught.exception))

    def test_dns_tls_timeout_and_disk_failures_are_distinguished(self):
        cases = [(updater.urllib.error.URLError(updater.socket.gaierror(-2, "private-host")), "DNS"),
                 (updater.urllib.error.URLError(updater.ssl.SSLCertVerificationError(1, "private-certificate")), "证书"),
                 (updater.urllib.error.URLError(TimeoutError()), "超时"),
                 (TimeoutError(), "超时"), (PermissionError(updater.errno.EACCES, "private-path"), "写入权限")]
        for error, expected in cases:
            opener = Mock(open=Mock(side_effect=error))
            with self.subTest(expected=expected), patch.object(updater.urllib.request, "build_opener", return_value=opener):
                with self.assertRaisesRegex(updater.UpdateError, expected) as caught:
                    updater.download(updater.RELEASE_API, limit=100)
                self.assertNotIn("private", str(caught.exception))

    def test_periodic_checks_run_without_analysis_or_an_open_page(self):
        manager = updater.UpdateManager(self.root)
        checked_again = threading.Event()
        calls = []

        def check():
            calls.append(True)
            if len(calls) >= 2:
                checked_again.set()

        manager.check = Mock(side_effect=check)
        manager.install = Mock()
        manager.start_periodic_checks(interval=0.02)
        thread = manager._periodic_thread
        try:
            manager.start_periodic_checks(interval=0.02)
            self.assertIs(manager._periodic_thread, thread)
            self.assertTrue(checked_again.wait(timeout=2))
        finally:
            manager.stop_periodic_checks()
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(manager.check.call_count, 2)
        manager.install.assert_not_called()
        self.assertEqual(manager.snapshot()["auto_check_interval_seconds"], 3600)

    def test_probe_restart_defers_immediate_check_but_keeps_periodic_schedule(self):
        manager = updater.UpdateManager(self.root)
        called = threading.Event()
        manager.check = Mock(side_effect=called.set)
        manager.start_periodic_checks(check_immediately=False, interval=0.02)
        try:
            self.assertTrue(called.wait(timeout=2))
        finally:
            manager.stop_periodic_checks()

    def test_periodic_thread_stops_without_waiting_for_the_hour_to_elapse(self):
        manager = updater.UpdateManager(self.root)
        manager.check = Mock()
        manager.start_periodic_checks(check_immediately=False)
        manager.stop_periodic_checks()
        manager.check.assert_not_called()
        self.assertFalse(manager._periodic_thread.is_alive())

    def test_install_cannot_race_with_a_background_check(self):
        manager = updater.UpdateManager(self.root, shutdown=Mock())
        manager.candidate = (*self.signed(), self.doc)
        manager.checking = True
        with self.assertRaisesRegex(updater.UpdateError, "正在检查更新"):
            manager.install("2.2.1")
        self.assertFalse(manager.installing)

    def test_installation_flag_always_keeps_analysis_blocked(self):
        manager = updater.UpdateManager(self.root)
        manager.state = {"state": "current"}
        manager.installing = True
        self.assertTrue(manager.snapshot()["busy"])

    def test_first_start_checks_even_during_first_minute_of_system_uptime(self):
        manager = updater.UpdateManager(self.root)
        with patch.object(updater.time, "monotonic", return_value=5), patch.object(updater.threading, "Thread") as thread:
            manager.check()
        thread.return_value.start.assert_called_once()
        self.assertEqual(manager.snapshot()["state"], "checking")


if __name__ == "__main__":
    unittest.main()
