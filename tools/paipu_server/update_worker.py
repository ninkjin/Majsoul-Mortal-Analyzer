"""Independent, copied worker: transactional replacement and startup rollback."""
import argparse
import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from app_updater import (
    UpdateError, app_info, atomic_json, dependency_path, hidden_process_options,
    managed_file, read_json, safe_target, update_environment, verify_manifest,
)


@contextmanager
def install_lock(root, name="install.lock"):
    if name not in ("install.lock", "server.lock"):
        raise UpdateError("锁文件名无效。")
    folder = Path(root) / ".updates"
    if folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()):
        raise UpdateError("更新目录不能是符号链接或目录联接。")
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            if not path.stat().st_size:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise UpdateError("另一更新程序正在运行，请稍后启动。") from None
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def transaction_dir(root, token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise UpdateError("更新事务标识无效。")
    path = Path(root) / ".updates" / token
    for item in (path.parent, path):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise UpdateError("更新目录包含不安全的链接。")
    if Path(root).resolve() not in path.resolve().parents:
        raise UpdateError("更新事务目录越界。")
    return path


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_stage(root, base):
    plan = read_json(base / "plan.json")
    trust = read_json(Path(root) / "update-trust.json")
    if plan["public_key"] != trust["public_key"]:
        raise UpdateError("发布公钥在更新期间发生变化，已停止。")
    manifest = verify_manifest((base / "manifest.json").read_bytes(), (base / "manifest.sig").read_bytes(),
                               trust["public_key"], current=plan["current"])
    for name, info in manifest["files"].items():
        path = safe_target(base / "app", name)
        if not path.is_file() or path.stat().st_size != info["size"] or file_hash(path) != info["sha256"]:
            raise UpdateError("暂存更新文件发生变化，已停止。")
    return plan, manifest


def prepare(root, base):
    _, manifest = verify_stage(root, base)
    dependencies = dependency_path(root)
    wheels = [base / "app" / name for name in manifest["files"] if name.startswith("update-wheels/")]
    if wheels:
        from packaging.utils import parse_wheel_filename
        target = base / "dependencies"
        cache = base / "wheel-cache"
        cache.mkdir()
        selected = {}
        if dependencies:
            previous_cache = dependencies.parent / "wheel-cache"
            records = read_json(dependencies.parent / "wheel-cache.json")
            for name, digest in records.items():
                source = safe_target(previous_cache, name)
                if file_hash(source) != digest:
                    raise UpdateError("已安装依赖的离线备份校验失败。")
                selected[parse_wheel_filename(name)[0]] = source
        fresh_names = set()
        for wheel in wheels:
            distribution = parse_wheel_filename(wheel.name)[0]
            if distribution in fresh_names:
                raise UpdateError("更新包包含同一依赖的多个版本。")
            fresh_names.add(distribution)
            selected[distribution] = wheel
        for wheel in selected.values():
            shutil.copy2(wheel, cache / wheel.name)
        records = {wheel.name: file_hash(wheel) for wheel in cache.glob("*.whl")}
        atomic_json(base / "wheel-cache.json", records)
        target.mkdir()
        # Rebuild the cumulative overlay, avoiding stale dist-info and removed files
        # left behind by pip --target --upgrade over an existing installation.
        command = [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile",
                   "--disable-pip-version-check", "--target", str(target), *map(str, cache.glob("*.whl"))]
        subprocess.run(command, check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=update_environment(root, target), **hidden_process_options())
        dependencies = target
    for name in manifest["files"]:
        if name.endswith(".py"):
            path = base / "app" / name
            compile(path.read_bytes(), name, "exec")
    subprocess.run([sys.executable, "-B", str(base / "app/tools/paipu_server/update_bootstrap.py"), "--self-test"],
                   check=True, timeout=90, env=update_environment(root, dependencies),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_process_options())
    atomic_json(base / "prepared.json", {"version": manifest["version"], "has_dependencies": bool(wheels)})


def journal_target(root, name):
    if name == ".updates/active-deps.json":
        return Path(root) / name
    if not managed_file(name):
        raise UpdateError("恢复清单包含非程序文件，已停止。")
    return safe_target(root, name)


def durable_copy(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".mortal-update-tmp")
    with Path(source).open("rb") as inp, temporary.open("wb") as out:
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, target)


def apply_files(root, base, manifest):
    paths = sorted(name for name in manifest["files"] if not name.startswith("update-wheels/"))
    paths += manifest.get("delete", [])
    if read_json(base / "prepared.json")["has_dependencies"]:
        paths += [".updates/active-deps.json"]
    entries = []
    for index, name in enumerate(paths):
        path = journal_target(root, name)
        if path.exists() and not path.is_file():
            raise UpdateError("更新目标不是普通文件，已停止。")
        entry = {"path": name, "existed": path.exists()}
        if entry["existed"]:
            backup = base / "backup" / str(index)
            durable_copy(path, backup)
            entry["sha256"] = file_hash(backup)
        entries.append(entry)
    # Journal and all backups are durable before the first live replacement.
    journal = {"phase": "applying", "entries": entries}
    atomic_json(base / "journal.json", journal)
    for entry in entries:
        name, target = entry["path"], journal_target(root, entry["path"])
        if name == ".updates/active-deps.json":
            atomic_json(target, {"transaction": base.name})
        elif name in manifest.get("delete", []):
            target.unlink(missing_ok=True)
        else:
            durable_copy(base / "app" / name, target)
    journal["phase"] = "checking"
    atomic_json(base / "journal.json", journal)


def rollback(root, base):
    journal = read_json(base / "journal.json", {})
    if journal.get("phase") in (None, "committed", "rolled_back"):
        return
    # Validate every backup before restoring any file.
    for index, entry in enumerate(journal["entries"]):
        journal_target(root, entry["path"])
        if entry["existed"] and file_hash(base / "backup" / str(index)) != entry["sha256"]:
            raise UpdateError("更新备份校验失败，请保留 .updates 目录并联系维护者。")
    for index, entry in reversed(list(enumerate(journal["entries"]))):
        target = journal_target(root, entry["path"])
        if entry["existed"]:
            durable_copy(base / "backup" / str(index), target)
        else:
            target.unlink(missing_ok=True)
    journal["phase"] = "rolled_back"
    atomic_json(base / "journal.json", journal)


def status(root, state, message):
    atomic_json(Path(root) / ".updates/status.json", {"state": state, "message": message})


def recover_pending(root):
    pending = Path(root) / ".updates/pending.json"
    if not pending.exists():
        return
    with install_lock(root):
        base = transaction_dir(root, read_json(pending)["transaction"])
        journal = read_json(base / "journal.json", {})
        rollback(root, base)
        if journal.get("phase") == "committed":
            status(root, "done", "更新已完成。")
        else:
            status(root, "rolled_back", "上次更新未完成，已恢复原版本，可以继续使用。")
        pending.unlink()


def wait_for_parent(pid, timeout=45):
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # already gone
                return
            raise UpdateError("无法确认旧程序是否退出，未替换任何文件。")
        try:
            if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise UpdateError("旧程序尚未退出，已取消更新。")
        finally:
            kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)
        raise UpdateError("旧程序尚未退出，已取消更新。")


def start_server(root, plan):
    if plan["host"] not in ("127.0.0.1", "localhost") or not 1 <= plan["port"] <= 65535:
        raise UpdateError("自动更新仅支持本机监听地址。")
    env = update_environment(root)
    env["MORTAL_UPDATE_HEALTH_TOKEN"] = plan["health_token"]
    return subprocess.Popen([sys.executable, "-B", str(Path(root) / "tools/paipu_server/update_bootstrap.py"),
                             "--host", plan["host"], "--port", str(plan["port"])], cwd=root, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **hidden_process_options())


def healthy(plan, version):
    connection = http.client.HTTPConnection("127.0.0.1", plan["port"], timeout=2)
    try:
        connection.request("GET", "/api/update/health")
        response = connection.getresponse()
        result = json.loads(response.read(16384))
        return response.status == 200 and result.get("version") == version and result.get("token") == plan["health_token"]
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def stop_owned_child(process):
    # Never kill a listener discovered by port: only terminate our own live child tree.
    if process is not None and process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **hidden_process_options())
        else:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)


def run_update(root, base, *, launch=start_server, health=healthy, wait_parent=wait_for_parent, deadline=60):
    plan, manifest = verify_stage(root, base)
    if app_info(root) != plan["current"]:
        raise UpdateError("本机版本已变化，请重新检查更新。")
    with install_lock(root):
        # Acquire the lock while waiting so a second launcher cannot recover an active transaction.
        wait_parent(plan["parent_pid"])
        pending = Path(root) / ".updates/pending.json"
        if read_json(pending, {}).get("transaction") != base.name:
            status(root, "error", "更新已被恢复操作取消，请重新启动程序。")
            return False
        child = None
        try:
            apply_files(root, base, manifest)
            status(root, "restarting", "更新文件已安装，正在验证启动…")
            child = launch(root, plan)
            until = time.monotonic() + deadline
            while time.monotonic() < until:
                if health(plan, manifest["version"]):
                    journal = read_json(base / "journal.json")
                    journal["phase"] = "committed"
                    atomic_json(base / "journal.json", journal)
                    status(root, "done", "更新完成，请刷新页面使用新版本。")
                    (Path(root) / ".updates/pending.json").unlink(missing_ok=True)
                    return True
                time.sleep(0.25)
            raise UpdateError("新版启动验证失败。")
        except Exception:
            stop_owned_child(child)
            rollback(root, base)
            status(root, "rolled_back", "更新未成功，已恢复原版本，请刷新页面继续使用。")
            (Path(root) / ".updates/pending.json").unlink(missing_ok=True)
            # The restored version starts without bypassing recovery.
            launch(root, plan)
            return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--transaction")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        if args.recover:
            recover_pending(root)
        elif args.prepare:
            prepare(root, transaction_dir(root, args.transaction))
        else:
            run_update(root, transaction_dir(root, args.transaction))
    except Exception:
        status(root, "recovery_required", "更新未完成。请关闭程序并重新启动；若仍失败，请保留 .updates 备份目录。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
