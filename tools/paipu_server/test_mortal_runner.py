import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mortal_runner
from mortal_runner import docker_command, model_file, runtime_python_candidates, write_local_config


class MortalRunnerTest(unittest.TestCase):
    def setUp(self):
        mortal_runner.clear_runtime_probe_cache()

    def tearDown(self):
        mortal_runner.clear_runtime_probe_cache()

    def test_runtime_candidates_prefer_portable_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            conda = root / ".conda"
            runtime.mkdir()
            conda.mkdir()
            (runtime / "python.exe").write_text("", encoding="utf-8")
            (conda / "python.exe").write_text("", encoding="utf-8")

            candidates = runtime_python_candidates(root)

            self.assertEqual(candidates[0], runtime / "python.exe")
            self.assertIn(conda / "python.exe", candidates)

    def test_write_local_config_points_to_model_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mortal").mkdir()
            (root / "mj_model").mkdir()
            (root / "mortal" / "config.example.toml").write_text(
                "state_file = '/path/to/mortal.pth'\n"
                "best_state_file = '/path/to/best.pth'\n"
                "tensorboard_dir = '/path/to/dir'\n"
                "device = 'cuda:0'\n"
                "state_file = '/path/to/grp.pth'\n",
                encoding="utf-8",
            )

            path = write_local_config(root)
            text = path.read_text(encoding="utf-8")

            self.assertIn("mj_model", text)
            self.assertIn("mortal.pth", text)
            self.assertIn("grp.pth", text)
            self.assertIn("device = 'cpu'", text)

    def test_write_local_config_accepts_selected_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mortal").mkdir()
            (root / "mj_model").mkdir()
            selected = root / "mj_model" / "custom.pth"
            selected.write_text("", encoding="utf-8")
            (root / "mortal" / "config.example.toml").write_text(
                "state_file = '/path/to/mortal.pth'\n"
                "best_state_file = '/path/to/best.pth'\n"
                "tensorboard_dir = '/path/to/dir'\n"
                "device = 'cuda:0'\n"
                "state_file = '/path/to/grp.pth'\n",
                encoding="utf-8",
            )

            path = write_local_config(root, selected)
            text = path.read_text(encoding="utf-8")

            self.assertIn("custom.pth", text)

    def test_model_file_prefers_model_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            self.assertEqual(model_file(root, "mortal.pth"), root / "mj_model" / "mortal.pth")

    def test_docker_command_uses_existing_volume_layout(self):
        root = Path("D:/mortalgame/Mortal")

        cmd = docker_command(root, 2, root / "mj_model" / "custom.pth")

        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("MORTAL_MODEL_PATH=/mnt/mj_model/custom.pth", cmd)
        self.assertIn("mortal:latest", cmd)
        self.assertEqual(cmd[-2:], ["tools/map_mortal_output.py", "2"])

    def test_successful_runtime_probes_are_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "python.exe"
            python.write_bytes(b"")
            dependency_result = Mock(
                returncode=0,
                stdout=str(Path(tmp) / "Lib" / "site-packages") + "\n",
                stderr="",
            )
            with patch.object(mortal_runner.subprocess, "run", return_value=dependency_result) as run:
                self.assertTrue(mortal_runner.python_has_mortal_deps(python))
                self.assertTrue(mortal_runner.python_has_mortal_deps(python))
                first_site = mortal_runner.python_site_packages(python)
                second_site = mortal_runner.python_site_packages(python)

        self.assertEqual(first_site, second_site)
        self.assertEqual(run.call_count, 1)

    def test_failed_runtime_probe_is_retried_after_dependencies_are_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "python.exe"
            python.write_bytes(b"")
            missing = Mock(returncode=1, stdout="", stderr="missing")
            available = Mock(returncode=0, stdout="ok\n", stderr="")
            with patch.object(mortal_runner.subprocess, "run", side_effect=[missing, available]) as run:
                self.assertFalse(mortal_runner.python_has_mortal_deps(python))
                self.assertTrue(mortal_runner.python_has_mortal_deps(python))
                self.assertTrue(mortal_runner.python_has_mortal_deps(python))

        self.assertEqual(run.call_count, 2)

    def test_failed_mapping_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mjai = root / "log.json"
            mapped = root / "mapped.jsonl"
            mjai.write_text("{}\n", encoding="utf-8")
            mapped.write_text("previous", encoding="utf-8")
            failure = mortal_runner.subprocess.CalledProcessError(
                returncode=1,
                cmd=["docker"],
                stderr=b"analysis failed",
            )
            with (
                patch.object(mortal_runner, "find_local_python", return_value=None),
                patch.object(mortal_runner.subprocess, "run", side_effect=failure),
            ):
                with self.assertRaisesRegex(RuntimeError, "analysis failed"):
                    mortal_runner.run_mortal_mapping(root, mjai, mapped, player_id=2)

            self.assertEqual(mapped.read_text(encoding="utf-8"), "previous")
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_successful_mapping_atomically_replaces_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mjai = root / "log.json"
            mapped = root / "mapped.jsonl"
            mjai.write_text("{}\n", encoding="utf-8")
            mapped.write_text("previous", encoding="utf-8")

            def successful_run(*_args, **kwargs):
                kwargs["stdout"].write(b"fresh")
                return Mock(returncode=0)

            with (
                patch.object(mortal_runner, "find_local_python", return_value=None),
                patch.object(mortal_runner.subprocess, "run", side_effect=successful_run),
            ):
                mortal_runner.run_mortal_mapping(root, mjai, mapped, player_id=2)

            self.assertEqual(mapped.read_bytes(), b"fresh")
            self.assertEqual(list(root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
