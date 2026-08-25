import json
import shutil
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

    def test_copy_outputs_keeps_previous_snapshot_when_staging_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "job"
            current = root / "viewer-data"
            work.mkdir()
            current.mkdir()
            tenhou = work / "source.json"
            mjai = work / "log.json"
            mapped = work / "mapped.jsonl"
            tenhou.write_text("new tenhou", encoding="utf-8")
            mjai.write_text("new mjai", encoding="utf-8")
            mapped.write_text("new mapped", encoding="utf-8")
            destinations = (
                current / "log.json",
                current / "mortal-output-p2-mapped.jsonl",
                current / "majsoul-tenhou-current.json",
            )
            for destination in destinations:
                destination.write_text("old", encoding="utf-8")

            real_copyfile = shutil.copyfile
            copy_count = 0

            def fail_second_copy(source, destination):
                nonlocal copy_count
                copy_count += 1
                if copy_count == 2:
                    raise OSError("disk full")
                return real_copyfile(source, destination)

            with (
                patch.object(server, "ROOT", root),
                patch.object(server, "CURRENT_DATA_DIR", current),
                patch.object(server.shutil, "copyfile", side_effect=fail_second_copy),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    server.copy_outputs(tenhou, mjai, mapped, player_id=2)

            self.assertEqual([path.read_text(encoding="utf-8") for path in destinations], ["old"] * 3)
            self.assertEqual(list(current.glob(".*.tmp")), [])

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

    def test_restore_history_rejects_result_paths_outside_job_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            job = jobs / "abc123"
            job.mkdir(parents=True)
            (root / "secret.json").write_text("secret", encoding="utf-8")
            (job / "log.json").write_text("mjai", encoding="utf-8")
            (job / "mapped.jsonl").write_text("mapped", encoding="utf-8")
            (job / "metadata.json").write_text(
                json.dumps({
                    "job_id": "abc123",
                    "files": {
                        "tenhou": "../../secret.json",
                        "mjai": "log.json",
                        "mapped": "mapped.jsonl",
                    },
                }),
                encoding="utf-8",
            )

            with patch.object(server, "PAIPU_JOBS_DIR", jobs):
                with self.assertRaisesRegex(ValueError, "文件名无效"):
                    server.restore_history("abc123")

    def test_history_list_skips_corrupt_metadata_and_invalid_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs"

            def write_job(name, metadata):
                job = jobs / name
                job.mkdir(parents=True)
                (job / "source.json").write_text("source", encoding="utf-8")
                (job / "log.json").write_text("mjai", encoding="utf-8")
                (job / "mapped.jsonl").write_text("mapped", encoding="utf-8")
                (job / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

            files = {"tenhou": "source.json", "mjai": "log.json", "mapped": "mapped.jsonl"}
            write_job("valid", {"job_id": "valid", "created_at": 100, "files": files})
            write_job("bad-time", {"job_id": "bad-time", "created_at": "not-a-time", "files": files})
            broken = jobs / "broken"
            broken.mkdir(parents=True)
            (broken / "metadata.json").write_text("[]", encoding="utf-8")

            with patch.object(server, "PAIPU_JOBS_DIR", jobs):
                items = server.completed_history_items(limit=5)

            self.assertEqual([item["job_id"] for item in items], ["valid"])


if __name__ == "__main__":
    unittest.main()
