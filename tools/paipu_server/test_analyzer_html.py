import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class AnalyzerHtmlTest(unittest.TestCase):
    def test_frontend_offers_majgg_and_tensoul_fetch_modes(self):
        html = server.ANALYZER_HTML

        self.assertIn('id="fetch-mode"', html)
        self.assertIn('<option value="majgg" selected>maj.gg 免登录获取</option>', html)
        self.assertIn('<option value="tensoul">tensoul 账号密码获取</option>', html)
        self.assertIn("fetch_method: fetchModeSelect.value", html)
        self.assertNotIn("https://mjai.ekyu.moe/zh-cn.html", html)
        self.assertNotIn("window.open(target", html)

    def test_frontend_offers_model_selector_from_mj_model_folder(self):
        html = server.ANALYZER_HTML

        self.assertIn('id="model-name"', html)
        self.assertIn("/api/models", html)
        self.assertIn("model_name: modelSelect.value", html)
        self.assertIn("loadModels();", html)


if __name__ == "__main__":
    unittest.main()
