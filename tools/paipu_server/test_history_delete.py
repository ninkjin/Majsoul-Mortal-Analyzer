import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server


class HistoryDeleteTest(unittest.TestCase):
    def test_copy_outputs_writes_current_viewer_data_folder_and_cleans_legacy_root_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "job"
            work.mkdir()
            tenhou = work / "source.json"
            mjai = work / "log.json"
            mapped = work / "mapped.jsonl"
            tenhou_text = '{"log":[]}'
            mjai_text = '{"type":"start_game"}\n'
            mapped_text = '{"reaction":{"type":"dahai"}}\n'
            tenhou.write_text(tenhou_text, encoding="utf-8")
            mjai.write_text(mjai_text, encoding="utf-8")
            mapped.write_text(mapped_text, encoding="utf-8")
            for name in server.LEGACY_CURRENT_OUTPUT_NAMES:
                (root / name).write_text("stale", encoding="utf-8")

            with (
                patch.object(server, "ROOT", root),
                patch.object(server, "CURRENT_DATA_DIR", root / "viewer-data"),
            ):
                server.copy_outputs(tenhou, mjai, mapped, player_id=2)

            self.assertEqual((root / "viewer-data" / "log.json").read_text(encoding="utf-8"), mjai_text)
            self.assertEqual((root / "viewer-data" / "mortal-output-p2-mapped.jsonl").read_text(encoding="utf-8"), mapped_text)
            self.assertEqual((root / "viewer-data" / "majsoul-tenhou-current.json").read_text(encoding="utf-8"), tenhou_text)
            self.assertEqual(json.loads((root / "viewer-data" / "mortal-viewer-config.json").read_text(encoding="utf-8")), {"player_id": 2})
            for name in server.LEGACY_CURRENT_OUTPUT_NAMES:
                self.assertFalse((root / name).exists())

    def test_delete_history_removes_safe_job_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            job = jobs / "abc123"
            job.mkdir(parents=True)
            (job / "metadata.json").write_text(json.dumps({"job_id": "abc123"}), encoding="utf-8")

            with patch.object(server, "PAIPU_JOBS_DIR", jobs):
                result = server.delete_history("abc123")

            self.assertEqual(result["job_id"], "abc123")
            self.assertFalse(job.exists())

    def test_delete_history_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs"
            jobs.mkdir()
            with patch.object(server, "PAIPU_JOBS_DIR", jobs):
                with self.assertRaises(FileNotFoundError):
                    server.delete_history("../outside")


if __name__ == "__main__":
    unittest.main()
