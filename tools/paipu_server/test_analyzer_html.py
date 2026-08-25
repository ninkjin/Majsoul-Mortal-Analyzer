import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class AnalyzerHtmlTest(unittest.TestCase):
    def test_local_api_key_config_is_never_served_as_a_static_file(self):
        self.assertTrue(server.static_path_is_private("/paipu-service.local.json"))
        self.assertTrue(server.static_path_is_private("/PAIPU-SERVICE.LOCAL.JSON"))
        self.assertTrue(server.static_path_is_private("/.env"))
        self.assertTrue(server.static_path_is_private("/.git/config"))
        self.assertTrue(server.static_path_is_private("/.GIT/config"))
        self.assertFalse(server.static_path_is_private("/mortal-output-viewer.html"))

    def test_static_file_allowlist_excludes_source_models_and_runtime(self):
        self.assertTrue(server.static_path_is_public("/mortal-output-viewer.html"))
        self.assertTrue(server.static_path_is_public("/viewer-data/log.json"))
        self.assertTrue(server.static_path_is_public("/log-viewer/files/images/blank.png"))
        self.assertFalse(server.static_path_is_public("/mj_model/mortal.pth"))
        self.assertFalse(server.static_path_is_public("/tools/paipu_server/server.py"))
        self.assertFalse(server.static_path_is_public("/runtime/python.exe"))
        self.assertFalse(server.static_path_is_public("/log-viewer/files/../../../mj_model/mortal.pth"))

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

    def test_history_values_are_rendered_as_text_instead_of_html(self):
        html = server.ANALYZER_HTML

        self.assertIn("summary.appendChild(document.createTextNode(shortPaipu(item.paipu)))", html)
        self.assertIn("details.textContent =", html)
        self.assertIn("showHistoryMessage(error.message)", html)
        self.assertNotIn("row.innerHTML", html)
        self.assertNotIn("${error.message}</div>", html)

    def test_status_polling_recovers_from_temporary_connection_errors(self):
        html = server.ANALYZER_HTML

        self.assertIn("async function poll(jobId, failures = 0)", html)
        self.assertIn("nextFailures <= 5", html)
        self.assertIn("setTimeout(() => poll(jobId, nextFailures), delay)", html)
        self.assertIn("startBtn.disabled = false", html)
        self.assertIn("passwordInput.value = ''", html)


if __name__ == "__main__":
    unittest.main()
