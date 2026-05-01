import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class FetchMethodTest(unittest.TestCase):
    def test_reward_calculator_is_loaded_from_mortal_file_path(self):
        module = server._load_reward_calculator_module()

        self.assertTrue(hasattr(module, "tenhou_log_to_mjai_log"))
        self.assertTrue(hasattr(module, "download_majsoul_tenhou_log"))

    def test_server_avoids_bare_reward_calculator_import_for_pylance(self):
        source = Path(server.__file__).read_text(encoding="utf-8")

        self.assertNotIn("from reward_calculator import", source)

    def run_job_with_patches(self, fetch_method):
        calls = []

        def fake_majgg(paipu, source_path, mjai_path, tenhou_summary_path=None):
            calls.append("majgg")
            source_path.write_text(json.dumps({
                "Game": {
                    "accounts": [
                        {"accountId": 18920167, "nickname": "lastkasd"},
                    ]
                }
            }), encoding="utf-8")
            mjai_path.write_text("[]", encoding="utf-8")
            if tenhou_summary_path:
                tenhou_summary_path.write_text(json.dumps({"log": []}), encoding="utf-8")

        def fake_tensoul(url, source_path, username=None, password=None):
            calls.append("tensoul")
            source_path.write_text(json.dumps({"name": ["a", "b", "c", "d"]}), encoding="utf-8")

        def fake_convert(source_path, mjai_path, player_id):
            calls.append("convert")
            mjai_path.write_text("[]", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp) / "jobs"
            with (
                patch.object(server, "PAIPU_JOBS_DIR", jobs_dir),
                patch.object(server, "download_with_majgg", fake_majgg),
                patch.object(server, "download_with_tensoul", fake_tensoul),
                patch.object(server, "convert_tenhou_to_mjai", fake_convert),
                patch.object(server, "run_mortal_mapping", lambda mjai, mapped, player, model_name="mortal.pth": mapped.write_text("{}", encoding="utf-8")),
                patch.object(server, "copy_outputs", lambda *args, **kwargs: None),
            ):
                with server.JOBS_LOCK:
                    server.JOBS["job"] = {
                        "job_id": "job",
                        "status": "queued",
                        "step": "排队中",
                        "progress": 0,
                        "created_at": 0,
                        "updated_at": 0,
                    }

                server.analyze_job(
                    "job",
                    "https://game.maj-soul.com/1/?paipu=260213-b409422a-54ad-4699-a9fe-23f9ed4c2390_a48969976",
                    "0",
                    username="u",
                    password="p",
                    fetch_method=fetch_method,
                    model_name="custom.pth",
                )

        return calls, server.JOBS["job"]

    def test_majgg_method_uses_only_majgg_download(self):
        calls, job = self.run_job_with_patches("majgg")

        self.assertEqual(calls, ["majgg"])
        self.assertEqual(job["status"], "done")

    def test_tensoul_method_skips_majgg_and_converts_tenhou_log(self):
        calls, job = self.run_job_with_patches("tensoul")

        self.assertEqual(calls, ["tensoul", "convert"])
        self.assertEqual(job["status"], "done")

    def test_available_model_names_are_listed_from_model_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_dir = root / "mj_model"
            model_dir.mkdir()
            (model_dir / "b.pth").write_text("", encoding="utf-8")
            (model_dir / "a.pth").write_text("", encoding="utf-8")
            (model_dir / "note.txt").write_text("", encoding="utf-8")
            with patch.object(server, "MODEL_DIR", model_dir):
                self.assertEqual(server.available_model_names(), ["a.pth", "b.pth"])

    def test_resolve_model_path_rejects_paths_outside_model_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "mj_model"
            model_dir.mkdir()
            (model_dir / "mortal.pth").write_text("", encoding="utf-8")
            with patch.object(server, "MODEL_DIR", model_dir):
                with self.assertRaises(ValueError):
                    server.resolve_model_path("../mortal.pth")

                self.assertEqual(server.resolve_model_path("mortal.pth"), (model_dir / "mortal.pth").resolve())


if __name__ == "__main__":
    unittest.main()
