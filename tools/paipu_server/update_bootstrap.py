"""Stable launcher: recover interrupted updates before importing the application."""
import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def activate_dependencies():
    from app_updater import dependency_path
    path = dependency_path(ROOT)
    if path:
        sys.path.insert(0, str(path))
        os.environ["PYTHONPATH"] = str(path)


def self_test():
    import importlib
    import importlib.metadata
    from packaging.requirements import Requirement
    for line in (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        if importlib.metadata.version(requirement.name) not in requirement.specifier:
            raise RuntimeError("Runtime dependency version mismatch")
    for module in ("torch", "mahjong", "tensoul", "numpy", "UnityPy", "cryptography", "libriichi.mjai", "server"):
        importlib.import_module(module)


def main():
    if "--self-test" in sys.argv:
        self_test()  # prepared overlay is supplied through PYTHONPATH
        return
    pending = ROOT / ".updates/pending.json"
    if pending.exists():
        token = json.loads(pending.read_text(encoding="utf-8"))["transaction"]
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise RuntimeError("Invalid pending update")
        base = ROOT / ".updates" / token
        plan = json.loads((base / "plan.json").read_text(encoding="utf-8"))
        probe = os.environ.get("MORTAL_UPDATE_HEALTH_TOKEN")
        if not probe or probe != plan["health_token"]:
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            subprocess.run([sys.executable, "-B", str(base / "update_worker.py"), "--root", str(ROOT), "--recover"],
                           check=True, timeout=90, **flags)
    if "--recover-only" in sys.argv:
        return
    activate_dependencies()
    if "--check-deps" in sys.argv:
        self_test()
        return
    runpy.run_path(str(ROOT / "tools/paipu_server/server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
