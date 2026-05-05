import os
import subprocess
import sys
from pathlib import Path


def runtime_python_candidates(root):
    root = Path(root)
    return [
        root / "runtime" / "python.exe",
        root / "runtime" / "Scripts" / "python.exe",
        root / ".runtime" / "python.exe",
        root / ".runtime" / "Scripts" / "python.exe",
        root / ".conda" / "python.exe",
        root / ".venv" / "Scripts" / "python.exe",
    ]


def find_local_python(root):
    forced = os.environ.get("MORTAL_LOCAL_PYTHON")
    candidates = [Path(forced)] if forced else []
    candidates.extend(runtime_python_candidates(root))
    for python in candidates:
        if python.exists() and python_has_mortal_deps(python):
            return python
    return None


def python_has_mortal_deps(python):
    code = "import torch; from libriichi.mjai import Bot; print('ok')"
    try:
        result = subprocess.run(
            [str(python), "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def python_site_packages(python):
    code = "import sysconfig; print(sysconfig.get_path('purelib'))"
    try:
        result = subprocess.run(
            [str(python), "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip()
    return path or None


def model_file(root, name):
    return Path(root) / "mj_model" / name


def write_local_config(root, selected_model=None):
    root = Path(root)
    source = root / "mortal" / "config.example.toml"
    target = root / "tmp" / "runtime-config.toml"
    control_model = Path(selected_model) if selected_model else model_file(root, "mortal.pth")
    text = source.read_text(encoding="utf-8")
    replacements = [
        ("state_file = '/path/to/mortal.pth'", f"state_file = '{_toml_path(control_model)}'"),
        ("best_state_file = '/path/to/best.pth'", f"best_state_file = '{_toml_path(model_file(root, 'best.pth'))}'"),
        ("tensorboard_dir = '/path/to/dir'", f"tensorboard_dir = '{_toml_path(root / 'tmp' / 'tensorboard')}'"),
        ("device = 'cuda:0'", "device = 'cpu'"),
        ("state_file = '/path/to/grp.pth'", f"state_file = '{_toml_path(model_file(root, 'grp.pth'))}'"),
    ]
    for old, new in replacements:
        text = text.replace(old, new, 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _toml_path(path):
    return str(Path(path).resolve()).replace("\\", "\\\\")


def _docker_model_path(root, model_path):
    root = Path(root).resolve()
    model_path = Path(model_path).resolve()
    try:
        rel = model_path.relative_to(root)
    except ValueError:
        rel = Path("mj_model") / model_path.name
    return "/mnt/" + rel.as_posix()


def docker_command(root, player_id, model_path=None):
    root = Path(root).resolve()
    model_path = Path(model_path) if model_path else model_file(root, "mortal.pth")
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        "-v",
        f"{root}:/mnt",
        "-v",
        f"{root / 'tools'}:/mortal/tools",
        "-e",
        f"MORTAL_MODEL_PATH={_docker_model_path(root, model_path)}",
        "--entrypoint",
        "python",
        "mortal:latest",
        "tools/map_mortal_output.py",
        str(player_id),
    ]


def local_command(root, python, player_id):
    return [str(python), str(Path(root) / "tools" / "map_mortal_output.py"), str(player_id)]


def run_mortal_mapping(root, mjai_path, mapped_path, player_id, model_path=None):
    root = Path(root).resolve()
    model_path = Path(model_path) if model_path else model_file(root, "mortal.pth")
    local_python = find_local_python(root)
    if local_python:
        config_path = write_local_config(root, model_path)
        env = os.environ.copy()
        env["MORTAL_CFG"] = str(config_path)
        env["MORTAL_MODEL_PATH"] = str(model_path)
        pythonpath_parts = []
        site_packages = python_site_packages(local_python)
        if site_packages:
            pythonpath_parts.append(site_packages)
        pythonpath_parts.append(str(root / "mortal"))
        if env.get("PYTHONPATH"):
            pythonpath_parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        cmd = local_command(root, local_python, player_id)
    else:
        env = None
        cmd = docker_command(root, player_id, model_path)

    with Path(mjai_path).open("rb") as stdin, Path(mapped_path).open("wb") as stdout:
        try:
            subprocess.run(cmd, cwd=root, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, env=env, check=True)
        except FileNotFoundError as exc:
            raise RuntimeError(_missing_runtime_message()) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
            if "dockerDesktopLinuxEngine" in stderr or "Cannot connect to the Docker daemon" in stderr:
                raise RuntimeError(_missing_runtime_message()) from exc
            raise RuntimeError(f"Mortal 分析失败：{stderr.strip() or exc}") from exc


def _missing_runtime_message():
    return (
        "找不到可用的 Mortal 运行环境。请先安装便携 runtime，或启动 Docker Desktop "
        "并确认 mortal:latest 镜像存在。"
    )
