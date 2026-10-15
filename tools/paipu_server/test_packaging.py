import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PortablePackagingTest(unittest.TestCase):
    def test_package_includes_license_and_offline_readme(self):
        script = (ROOT / "scripts" / "package-portable.ps1").read_text(encoding="utf-8-sig")

        self.assertIn('"README.md"', script)
        self.assertIn('"LICENSE"', script)
        self.assertIn('"scripts"', script)

    def test_build_tool_is_installed_for_setup_but_removed_from_package(self):
        requirements = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8-sig").splitlines()
        setup = (ROOT / "scripts" / "setup-runtime.ps1").read_text(encoding="utf-8-sig")
        package = (ROOT / "scripts" / "package-portable.ps1").read_text(encoding="utf-8-sig")

        self.assertNotIn("maturin", requirements)
        self.assertIn("-m pip install maturin", setup)
        self.assertIn("Remove-StagedBuildTool", package)
        self.assertIn("Get-Command tar.exe", package)
        self.assertIn("zip:hdrcharset=UTF-8", package)

    def test_public_release_defaults_to_anonymous_desktop_access(self):
        example = json.loads(
            (ROOT / "paipu-service.example.json").read_text(encoding="utf-8-sig")
        )
        fetcher = (ROOT / "tools" / "paipu_server" / "remote_service_fetcher.py").read_text(
            encoding="utf-8-sig"
        )

        self.assertEqual(example["api_key"], "")
        self.assertIn("/api/v1/desktop/requests", fetcher)

    def test_portable_release_includes_version_and_only_public_signing_key(self):
        import base64
        script = (ROOT / "scripts/package-portable.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('"app-version.json"', script)
        self.assertIn('"update-trust.json"', script)
        self.assertNotIn('".updates"', script)
        trust = json.loads((ROOT / "update-trust.json").read_text(encoding="utf-8"))
        self.assertEqual(len(base64.b64decode(trust["public_key"], validate=True)), 32)
        self.assertNotIn("private", trust)
        launcher = (ROOT / "启动雀魂分析器.cmd").read_text(encoding="utf-8-sig")
        self.assertIn("update_bootstrap.py", launcher)
        helper = (ROOT / "scripts/ensure-local-server.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("ensure-local-server.ps1", launcher)
        self.assertIn("tools\\paipu_server\\app_updater.py", helper)
        self.assertIn("tools\\paipu_server\\update_worker.py", helper)
        self.assertIn("analysis_busy", helper)
        self.assertIn("[datetime]$process.CreationDate", helper)
        self.assertIn("-WindowStyle Hidden", helper)
        self.assertIn("--recover-only", launcher)

    def test_packaging_preserves_old_outputs_and_installs_only_into_stage(self):
        script = (ROOT / "scripts/package-portable.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Output already exists", script)
        self.assertNotIn("Remove-Item -LiteralPath $stage -Recurse", script)
        self.assertNotIn("Remove-Item -LiteralPath $zip", script)
        self.assertIn("$stagedPython -E -B -m pip install", script)
        self.assertNotIn("$python -m pip install", script)
        self.assertIn("pyvenv.cfg", script)
        self.assertIn('Join-Path $stagedRuntime "etc\\aau_token"', script)
        self.assertIn('Join-Path $stage "mj_model\\mortal.pth"', script)


if __name__ == "__main__":
    unittest.main()
