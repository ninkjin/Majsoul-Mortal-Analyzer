import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mortal_runner import docker_command, model_file, runtime_python_candidates, write_local_config


class MortalRunnerTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
