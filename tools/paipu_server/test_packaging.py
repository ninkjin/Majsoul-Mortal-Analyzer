import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PortablePackagingTest(unittest.TestCase):
    def test_package_includes_license_and_offline_readme(self):
        script = (ROOT / "scripts" / "package-portable.ps1").read_text(encoding="utf-8-sig")

        self.assertIn('"README.md"', script)
        self.assertIn('"LICENSE"', script)

    def test_build_tool_is_installed_for_setup_but_removed_from_package(self):
        requirements = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8-sig").splitlines()
        setup = (ROOT / "scripts" / "setup-runtime.ps1").read_text(encoding="utf-8-sig")
        package = (ROOT / "scripts" / "package-portable.ps1").read_text(encoding="utf-8-sig")

        self.assertNotIn("maturin", requirements)
        self.assertIn("-m pip install maturin", setup)
        self.assertIn("Remove-StagedBuildTool", package)
        self.assertIn("Get-Command tar.exe", package)


if __name__ == "__main__":
    unittest.main()
