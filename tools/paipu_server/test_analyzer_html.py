import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class AnalyzerHtmlTest(unittest.TestCase):
    def test_local_api_key_config_is_never_served_as_a_static_file(self):
        self.assertTrue(server.static_path_is_private("/paipu-service.local.json"))
        self.assertTrue(server.static_path_is_private("/.env"))
        self.assertTrue(server.static_path_is_private("/.git/config"))
        self.assertFalse(server.static_path_is_private("/mortal-output-viewer.html"))

    def test_frontend_offers_remote_service_and_tensoul_fetch_modes(self):
        html = server.ANALYZER_HTML

        self.assertIn('id="fetch-mode"', html)
        self.assertIn('<option value="remote" selected>ninklang.tech 在线获取（推荐）</option>', html)
        self.assertIn('<option value="tensoul">tensoul 本地账号密码获取</option>', html)
        self.assertIn("fetch_method: fetchModeSelect.value", html)
        self.assertNotIn("maj.gg", html)
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
