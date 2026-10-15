"""Signed application-only updates. Never replace personal data or the base runtime.

The publisher's key is pinned in update-trust.json, not downloaded with an update.
Downloads are untrusted until the manifest signature and every file digest pass.
"""
import base64
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

REPOSITORY = "ninkjin/Majsoul-Mortal-Analyzer"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
MAX_PACKAGE = 128 * 1024 * 1024
MAX_EXPANDED = 384 * 1024 * 1024
MAX_FILES = 2000
PROTOCOL = 1
AUTO_CHECK_INTERVAL = 60 * 60
BUSY_STATES = {"downloading", "preparing", "installing", "restarting", "recovering"}
ROOT_FILES = {
    "app-version.json", "requirements-runtime.txt", "README.md", "LICENSE",
    "mortal-output-viewer.html", "majsoul-paipu-fetcher.html", "paipu-service.example.json",
    "start-paipu-server.ps1", "启动雀魂分析器.cmd", "检查环境.cmd",
}
REQUIRED_FILES = {
    "app-version.json", "requirements-runtime.txt", "tools/paipu_server/server.py",
    "tools/paipu_server/update_bootstrap.py", "tools/paipu_server/app_updater.py",
    "tools/paipu_server/update_worker.py", "启动雀魂分析器.cmd",
    "scripts/ensure-local-server.ps1",
}


class UpdateError(RuntimeError):
    pass


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})", value):
        raise UpdateError("更新版本号格式无效。")
    return tuple(map(int, value.split(".")))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def app_info(root):
    info = read_json(Path(root) / "app-version.json", {"version": "2.1.3", "runtime_abi": "unknown"})
    version_tuple(info["version"])
    return info


def safe_name(name):
    if not isinstance(name, str) or not name or len(name) > 240 or "\\" in name or ":" in name:
        raise UpdateError("更新包包含无效路径。")
    parts = PurePosixPath(name).parts
    if not parts or name != "/".join(parts) or name.startswith("/"):
        raise UpdateError("更新包路径不规范。")
    for part in parts:
        if (part in (".", "..") or part.endswith((".", " ")) or part.startswith(".")
                or any(ord(c) < 32 for c in part)
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)):
            raise UpdateError("更新包包含不安全的路径。")
    return parts


def managed_file(name):
    parts = safe_name(name)
    if name in ROOT_FILES:
        return True
    if len(parts) >= 2 and parts[0] == "mortal":
        return name.endswith(".py") or name == "mortal/config.example.toml"
    if len(parts) >= 3 and parts[:2] == ("tools", "paipu_server"):
        return name.endswith((".py", ".cjs"))
    if len(parts) == 2 and parts[0] == "tools":
        return name.endswith(".py")
    if len(parts) == 2 and parts[0] == "scripts":
        return name.endswith((".py", ".ps1"))
    if len(parts) >= 3 and parts[:2] == ("log-viewer", "files"):
        return Path(name).suffix.lower() in {".js", ".css", ".png", ".jpg", ".gif", ".svg", ".woff", ".woff2"}
    return False


def safe_target(root, name):
    safe_name(name)
    root = Path(root).resolve()
    target = root.joinpath(*PurePosixPath(name).parts)
    for item in [target, *target.parents]:
        if item == root:
            break
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise UpdateError("更新目标包含符号链接或目录联接，已停止。")
    if root not in target.resolve().parents:
        raise UpdateError("更新目标超出程序目录。")
    return target


def checked_download_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {
            "api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
            or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment):
        raise UpdateError("更新下载地址未通过安全检查。")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, *, limit, destination=None, expected_hash=None, expected_size=None, progress=None):
    checked_download_url(url)
    opener = urllib.request.build_opener(SafeRedirect())
    # Release metadata is a JSON API; octet-stream is only for release asset files.
    # GitHub rejects octet-stream on /releases/latest with HTTP 415.
    accept = "application/vnd.github+json" if url == RELEASE_API else "application/octet-stream"
    request = urllib.request.Request(url, headers={"User-Agent": "MortalAnalyzer-Updater/1", "Accept": accept})
    started, size, digest = time.monotonic(), 0, hashlib.sha256()
    chunks = []
    output = None
    try:
        if destination:
            output = Path(destination).open("xb")
        with opener.open(request, timeout=15) as response:
            checked_download_url(response.url)
            while chunk := response.read(64 * 1024):
                size += len(chunk)
                if size > limit or time.monotonic() - started > 180:
                    raise UpdateError("更新下载超时或文件过大，旧版本不受影响。")
                digest.update(chunk)
                if output:
                    output.write(chunk)
                else:
                    chunks.append(chunk)
                if progress:
                    progress(size)
        if expected_size is not None and size != expected_size:
            raise UpdateError("更新包大小不匹配，已停止更新。")
        if expected_hash and digest.hexdigest() != expected_hash:
            raise UpdateError("更新包校验失败，已停止更新。")
        return b"".join(chunks) if output is None else None
    except urllib.error.HTTPError as exc:
        code = exc.code
        limited = code == 429 or (code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0")
        exc.close()
        if limited:
            message = f"更新检查次数暂时受限（HTTP {code}），请稍后重试；当前版本可继续使用。"
        elif code in (406, 415):
            message = f"更新服务拒绝了请求格式（HTTP {code}），请联系维护者修复更新组件。"
        elif code == 404:
            message = "未找到发行信息或更新文件（HTTP 404），请联系维护者检查发布状态。"
        elif code >= 500:
            message = f"更新服务暂时异常（HTTP {code}），请稍后重试；当前版本可继续使用。"
        else:
            message = f"更新请求被拒绝（HTTP {code}）；当前版本可继续使用。"
        raise UpdateError(message) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            message = "更新连接的证书验证失败，请检查系统时间或代理；未安装任何文件。"
        elif isinstance(exc.reason, socket.gaierror):
            message = "无法解析更新服务地址，请检查网络或 DNS；当前版本可继续使用。"
        elif isinstance(exc.reason, TimeoutError):
            message = "连接更新服务超时，请稍后重试；当前版本可继续使用。"
        else:
            message = "暂时无法连接更新服务，请检查网络或代理；当前版本可继续使用。"
        raise UpdateError(message) from None
    except TimeoutError:
        raise UpdateError("更新下载超时，请稍后重试；当前版本可继续使用。") from None
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.ENOSPC, errno.EROFS):
            raise UpdateError("无法保存更新文件，请检查安装目录的写入权限和剩余磁盘空间。") from None
        raise UpdateError("更新下载连接中断或本机文件读写失败，请稍后重试。") from None
    finally:
        if output:
            output.close()


def verify_manifest(raw, signature, public_key, *, current, now=None):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(
            base64.b64decode(signature, validate=True), raw,
        )
    except (InvalidSignature, ValueError, TypeError):
        raise UpdateError("更新签名验证失败，未安装任何文件。") from None
    try:
        doc = json.loads(raw)
        if doc["schema"] != 1 or doc["updater_protocol"] > PROTOCOL:
            raise UpdateError("更新器版本不兼容，请先安装新版完整便携包。")
        if version_tuple(doc["version"]) <= version_tuple(current["version"]):
            raise UpdateError("已阻止安装旧版本或重复版本。")
        if doc["runtime_abi"] != current["runtime_abi"]:
            raise UpdateError("此版本需要更换 Python 运行环境，请安装新版完整便携包。")
        now = time.time() if now is None else now
        if not isinstance(doc["created"], int) or not isinstance(doc["expires"], int):
            raise ValueError()
        if not doc["created"] - 86400 <= now < doc["expires"] or doc["expires"] <= doc["created"]:
            raise UpdateError("更新清单已过期或系统时间不正确，请稍后重试。")
        expected_url = f"https://github.com/{REPOSITORY}/releases/download/v{doc['version']}/mortal-update-{doc['version']}.zip"
        if doc["url"] != expected_url or not 0 < doc["size"] <= MAX_PACKAGE:
            raise ValueError()
        if not re.fullmatch(r"[0-9a-f]{64}", doc["sha256"]):
            raise ValueError()
        files, deleted = doc["files"], doc.get("delete", [])
        if not isinstance(files, dict) or not REQUIRED_FILES.issubset(files) or len(files) > MAX_FILES:
            raise ValueError()
        seen, total = set(), 0
        for name, meta in files.items():
            safe_name(name)
            wheel = name.startswith("update-wheels/") and len(PurePosixPath(name).parts) == 2 and name.endswith(".whl")
            if not managed_file(name) and not wheel:
                raise UpdateError("更新包试图修改程序之外的文件，已停止。")
            if name.casefold() in seen or not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"]):
                raise ValueError()
            seen.add(name.casefold())
            if type(meta["size"]) is not int or not 0 <= meta["size"] <= MAX_PACKAGE:
                raise ValueError()
            total += meta["size"]
        if total > MAX_EXPANDED or not isinstance(deleted, list) or len(deleted) > MAX_FILES:
            raise ValueError()
        for name in deleted:
            if not managed_file(name) or name in REQUIRED_FILES or name.casefold() in seen:
                raise ValueError()
            seen.add(name.casefold())
        if not isinstance(doc.get("notes", ""), str) or len(doc.get("notes", "")) > 8000:
            raise ValueError()
        return doc
    except (KeyError, TypeError, ValueError, AttributeError):
        raise UpdateError("更新清单格式无效，已停止更新。") from None


def unpack_verified(archive, target, manifest):
    target = Path(target)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) != len(manifest["files"]):
            raise UpdateError("更新包文件清单不匹配。")
        seen = set()
        for entry in entries:
            name = entry.filename
            if name in seen or name not in manifest["files"] or entry.is_dir():
                raise UpdateError("更新包含重复或未声明文件。")
            seen.add(name)
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if kind not in (0, stat.S_IFREG) or entry.flag_bits & 1:
                raise UpdateError("更新包不允许链接、特殊文件或加密文件。")
            meta = manifest["files"][name]
            if entry.file_size != meta["size"]:
                raise UpdateError("更新包文件大小不匹配。")
            path = safe_target(target, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with package.open(entry) as source, path.open("xb") as output:
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    if size > meta["size"]:
                        raise UpdateError("更新包解压大小超出声明。")
                    digest.update(chunk)
                    output.write(chunk)
            if size != meta["size"] or digest.hexdigest() != meta["sha256"]:
                raise UpdateError("更新包内文件校验失败。")
    info = app_info(target)
    if info["version"] != manifest["version"] or info["runtime_abi"] != manifest["runtime_abi"]:
        raise UpdateError("更新包内版本信息与签名清单不一致。")


def hidden_process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}


def dependency_path(root):
    root = Path(root).resolve()
    state = read_json(root / ".updates/active-deps.json", {})
    token = state.get("transaction")
    if token is None:
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise UpdateError("依赖目录配置异常。")
    path = root / ".updates" / token / "dependencies"
    if not path.is_dir() or path.is_symlink() or root not in path.resolve().parents:
        raise UpdateError("更新依赖目录不存在或不安全。")
    return path


def update_environment(root, dependencies=None):
    env = os.environ.copy()
    path = dependencies if dependencies is not None else dependency_path(root)
    # The old overlay must not leak into validation of a newly prepared overlay.
    env["PYTHONPATH"] = str(path) if path else ""
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class UpdateManager:
    def __init__(self, root, shutdown=None, host="127.0.0.1", port=8765):
        self.root = Path(root).resolve()
        self.current = app_info(self.root)
        self.shutdown, self.host, self.port = shutdown, host, port
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.candidate = None
        self.checking = self.installing = False
        self._periodic_stop = threading.Event()
        self._periodic_thread = None
        self.last_check = 0
        self.state = {"state": "idle", "message": "尚未检查更新。"}
        saved = read_json(self.root / ".updates/status.json", {})
        if saved.get("state") in BUSY_STATES and not (self.root / ".updates/pending.json").exists():
            self.set_state("error", "上次更新下载已中断，原版本未修改；可以重新检查更新。")

    def snapshot(self):
        with self.lock:
            saved = read_json(self.root / ".updates/status.json", {})
            state = self.state if self.checking or self.installing or self.last_check else (saved or self.state)
            return {**state, "current_version": self.current["version"], "csrf_token": self.token,
                    "release_url": RELEASE_PAGE, "auto_check_interval_seconds": AUTO_CHECK_INTERVAL,
                    "busy": self.installing or state.get("state") in BUSY_STATES}

    def start_periodic_checks(self, *, check_immediately=True, interval=AUTO_CHECK_INTERVAL):
        """Runs while the local service runs, independently of pages and analysis jobs."""
        if interval <= 0:
            raise ValueError("Update check interval must be positive")
        with self.lock:
            if self._periodic_thread is not None and self._periodic_thread.is_alive():
                return
            self._periodic_stop.clear()

            def run():
                first = check_immediately
                while first or not self._periodic_stop.wait(interval):
                    first = False
                    if self._periodic_stop.is_set():
                        return
                    try:
                        self.check()
                    except Exception as exc:
                        # A transient local I/O failure must not kill future checks.
                        # Never log response bodies, proxy URLs, or credentials.
                        print(f"automatic update check deferred: {type(exc).__name__}", file=sys.stderr)

            self._periodic_thread = threading.Thread(target=run, daemon=True, name="mortal-periodic-update-check")
            self._periodic_thread.start()

    def stop_periodic_checks(self):
        self._periodic_stop.set()
        thread = self._periodic_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def set_state(self, state, message, **fields):
        with self.lock:
            self.state = {"state": state, "message": message, **fields}
            atomic_json(self.root / ".updates/status.json", self.state)

    def check(self):
        with self.lock:
            if self.checking or self.installing or self.snapshot()["busy"]:
                return self.snapshot()
            if (self.root / ".updates/pending.json").exists():
                self.set_state("recovery_required", "存在未完成的更新，请重新运行启动脚本以恢复。")
                return self.snapshot()
            if self.last_check and time.monotonic() - self.last_check < 60:
                return self.snapshot()
            self.checking = True
            self.set_state("checking", "正在检查软件更新…")
            threading.Thread(target=self._check, daemon=True, name="mortal-update-check").start()
            return self.snapshot()

    def _check(self):
        try:
            trust = read_json(self.root / "update-trust.json")
            if not trust.get("public_key"):
                raise UpdateError("尚未配置发布签名，软件内更新暂不可用。")
            release = json.loads(download(RELEASE_API, limit=2 * 1024 * 1024))
            tag = release.get("tag_name", "")
            if not tag.startswith("v") or release.get("draft") or release.get("prerelease"):
                raise UpdateError("更新服务没有提供可用的正式版本。")
            if version_tuple(tag[1:]) <= version_tuple(self.current["version"]):
                self.candidate = None
                self.set_state("current", "当前程序无需更新。")
                return
            assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
            prefix = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
            for name in ("mortal-update.json", "mortal-update.sig"):
                if assets.get(name) != prefix + name:
                    raise UpdateError("新版尚未提供签名更新包，请稍后再检查或下载完整便携包。")
            raw = download(assets["mortal-update.json"], limit=512 * 1024)
            signature = download(assets["mortal-update.sig"], limit=256)
            manifest = verify_manifest(raw, signature, trust["public_key"], current=self.current)
            if manifest["version"] != tag[1:]:
                raise UpdateError("发布版本与签名清单不一致。")
            self.candidate = (raw, signature, manifest)
            self.set_state("available", "发现新版本，可下载并重启更新。", available_version=manifest["version"],
                           notes=manifest.get("notes", ""), size=manifest["size"])
        except Exception as exc:
            self.candidate = None
            message = str(exc) if isinstance(exc, UpdateError) else "检查更新失败，当前版本可继续使用。"
            self.set_state("error", message)
        finally:
            with self.lock:
                self.last_check = time.monotonic()
                self.checking = False

    def install(self, expected_version):
        with self.lock:
            if self.checking:
                raise UpdateError("正在检查更新，请等待检查完成后再安装。")
            if self.installing or self.snapshot()["busy"]:
                raise UpdateError("更新正在进行，请勿重复操作。")
            if not self.candidate or self.candidate[2]["version"] != expected_version:
                raise UpdateError("请先检查并确认当前可用版本。")
            if self.shutdown is None:
                raise UpdateError("当前启动方式不支持自动重启。")
            if self.host not in ("127.0.0.1", "localhost"):
                raise UpdateError("软件内更新仅支持本机监听模式，请使用默认启动脚本。")
            if (self.root / ".git").exists():
                raise UpdateError("检测到开发者源码目录，请通过 Git 更新；软件内更新用于便携发行包。")
            if (self.root / ".updates/pending.json").exists():
                raise UpdateError("存在未完成的更新，请关闭程序后重新启动以恢复。")
            self.installing = True
            self.set_state("downloading", "正在下载更新，现有文件尚未修改…", available_version=expected_version)
            threading.Thread(target=self._prepare, daemon=True, name="mortal-update-download").start()
            return self.snapshot()

    def _prepare(self):
        try:
            raw, signature, _ = self.candidate
            trust = read_json(self.root / "update-trust.json")
            manifest = verify_manifest(raw, signature, trust["public_key"], current=self.current)
            transaction = secrets.token_hex(16)
            base = self.root / ".updates" / transaction
            base.mkdir(parents=True)
            archive = base / "package.zip"
            last_percent = -1

            def progress(size):
                nonlocal last_percent
                percent = int(size * 100 / manifest["size"])
                if percent != last_percent:
                    last_percent = percent
                    self.set_state("downloading", f"正在下载更新：{percent}%", available_version=manifest["version"])

            download(manifest["url"], limit=MAX_PACKAGE, destination=archive,
                     expected_hash=manifest["sha256"], expected_size=manifest["size"], progress=progress)
            self.set_state("preparing", "正在校验更新文件与依赖，现有文件尚未修改…")
            unpack_verified(archive, base / "app", manifest)
            for name in ("app_updater.py", "update_worker.py"):
                shutil.copy2(self.root / "tools/paipu_server" / name, base / name)
            (base / "manifest.json").write_bytes(raw)
            (base / "manifest.sig").write_bytes(signature)
            atomic_json(base / "plan.json", {"current": self.current, "public_key": trust["public_key"],
                        "parent_pid": os.getpid(), "host": self.host, "port": self.port,
                        "health_token": secrets.token_urlsafe(32)})
            # Dependency installation happens in an isolated overlay before the old server stops.
            result = subprocess.run([sys.executable, "-B", str(base / "update_worker.py"),
                                     "--root", str(self.root), "--transaction", transaction, "--prepare"],
                                    env=update_environment(self.root), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=240, **hidden_process_options())
            if result.returncode:
                raise UpdateError("更新依赖或兼容性检查失败，旧版本未修改。")
            atomic_json(self.root / ".updates/pending.json", {"transaction": transaction})
            self.set_state("installing", "下载和校验完成，正在重启安装；请勿关闭电脑。")
            subprocess.Popen([sys.executable, "-B", str(base / "update_worker.py"), "--root", str(self.root),
                              "--transaction", transaction], cwd=self.root, env=update_environment(self.root),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **hidden_process_options())
            self.shutdown()
        except Exception as exc:
            # No live files have been changed in this process.
            pending = self.root / ".updates/pending.json"
            if pending.exists():
                pending.unlink()
            message = str(exc) if isinstance(exc, UpdateError) else "更新准备失败，旧版本仍可继续使用。"
            self.set_state("error", message)
            with self.lock:
                self.installing = False
