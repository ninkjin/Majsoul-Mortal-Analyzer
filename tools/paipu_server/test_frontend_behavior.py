import json
import shutil
import subprocess
import unittest
from pathlib import Path

import server


NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(NODE, "Node.js is required for JavaScript behavior tests")
class FrontendBehaviorTest(unittest.TestCase):
    def test_browser_logic_regressions(self):
        result = subprocess.run(
            [NODE, str(Path(__file__).with_name("frontend.test.cjs"))],
            input=json.dumps({
                "viewer": (ROOT / "mortal-output-viewer.html").read_text(encoding="utf-8"),
                "analyzer": server.ANALYZER_HTML,
            }),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
