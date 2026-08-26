import argparse
import asyncio
import importlib.util
import json
import math
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import traceback
import shutil
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
PAIPU_JOBS_DIR = ROOT / "tmp" / "paipu_jobs"
CURRENT_DATA_DIR = ROOT / "viewer-data"
MODEL_DIR = ROOT / "mj_model"
DEFAULT_MODEL_NAME = "mortal.pth"
THREE_PLAYER_UNSUPPORTED_MESSAGE = "暂不支持三麻牌谱，请提交四麻东风战或半庄牌谱。"
LEGACY_CURRENT_OUTPUT_NAMES = (
    "log.json",
    "mortal-output-p2-mapped.jsonl",
    "mortal-output-p2.jsonl",
    "majsoul-tenhou-current.json",
    "mortal-viewer-config.json",
)
HISTORY_KEEP = 20
JOB_STATUS_KEEP = 100
MAX_PENDING_JOBS = 10
MAX_PAIPU_CHARS = 512
MAX_SAFE_NAME_CHARS = 120
MAX_REQUEST_BODY_BYTES = 64 * 1024
JOBS = {}
JOBS_LOCK = threading.Lock()
CURRENT_OUTPUTS_LOCK = threading.Lock()
_REWARD_CALCULATOR_MODULE = None
PRIVATE_STATIC_FILES = {".env", "paipu-service.local.json"}
PUBLIC_STATIC_FILES = {"majsoul-paipu-fetcher.html", "mortal-output-viewer.html"}
PUBLIC_VIEWER_FILES = {
    "log.json",
    "majsoul-tenhou-current.json",
    "mortal-output-p2-mapped.jsonl",
    "mortal-viewer-config.json",
}


class HttpRequestError(ValueError):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def extract_paipu(value):
    match = re.search(r"[?&]paipu=([^&#\s]+)", value or "") or re.search(r"^paipu=([^&#\s]+)", value or "")
    if not match:
        raise ValueError("没有找到 paipu 参数")
    paipu = urllib.parse.unquote(match.group(1))
    if len(paipu) > MAX_PAIPU_CHARS:
        raise ValueError("paipu 参数过长")
    if any(ord(character) < 32 for character in paipu):
        raise ValueError("paipu 参数包含控制字符")
    return paipu


def safe_name(value):
    name = re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("_")[:MAX_SAFE_NAME_CHARS]
    return name or "paipu"


def static_path_is_private(request_path):
    parts = (part.casefold() for part in Path(urllib.parse.unquote(request_path).lstrip("/")).parts)
    return any(part in PRIVATE_STATIC_FILES or part == ".git" for part in parts)


def static_path_is_public(request_path):
    parts = tuple(part.casefold() for part in Path(urllib.parse.unquote(request_path).lstrip("/")).parts)
    if ".." in parts:
        return False
    if len(parts) == 1:
        return parts[0] in PUBLIC_STATIC_FILES
    if len(parts) == 2 and parts[0] == "viewer-data":
        return parts[1] in PUBLIC_VIEWER_FILES
    return len(parts) >= 3 and parts[:2] == ("log-viewer", "files")


def available_model_names():
    if not MODEL_DIR.exists():
        return []
    return sorted(path.name for path in MODEL_DIR.glob("*.pth") if path.is_file())


def resolve_model_path(model_name=""):
    name = str(model_name or DEFAULT_MODEL_NAME)
    if Path(name).name != name or not name.endswith(".pth"):
        raise ValueError("模型文件必须是 mj_model 文件夹里的 .pth 文件")
    path = (MODEL_DIR / name).resolve()
    model_dir = MODEL_DIR.resolve()
    if model_dir not in path.parents:
        raise ValueError("模型文件必须位于 mj_model 文件夹")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"找不到模型文件：mj_model/{name}")
    return path


def account_id_from_paipu(paipu):
    match = re.search(r"_a(\d+)(?:_|$)", paipu or "")
    if not match:
        return None
    encoded = int(match.group(1))
    return (((encoded - 1358437) ^ 86216345) - 1117113) // 7


def set_job(job_id, **updates):
    with JOBS_LOCK:
        JOBS[job_id].update(updates)
        JOBS[job_id]["updated_at"] = time.time()


def prune_finished_jobs(keep=JOB_STATUS_KEEP):
    keep = max(0, int(keep))
    with JOBS_LOCK:
        finished = sorted(
            (
                (job_id, job)
                for job_id, job in JOBS.items()
                if job.get("status") in ("done", "error")
            ),
            key=lambda item: float(item[1].get("updated_at") or 0),
            reverse=True,
        )
        for job_id, _job in finished[keep:]:
            JOBS.pop(job_id, None)


def download_with_remote_service(url, tenhou_path, status_callback=None):
    from remote_service_fetcher import fetch_remote_tenhou

    return fetch_remote_tenhou(url, tenhou_path, status_callback=status_callback)


def _load_reward_calculator_module():
    global _REWARD_CALCULATOR_MODULE
    if _REWARD_CALCULATOR_MODULE is not None:
        return _REWARD_CALCULATOR_MODULE

    module_path = ROOT / "mortal" / "reward_calculator.py"
    spec = importlib.util.spec_from_file_location("mortal_reward_calculator", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _REWARD_CALCULATOR_MODULE = module
    return module


def convert_tenhou_to_mjai(tenhou_path, mjai_path, player_id):
    reward_calculator = _load_reward_calculator_module()

    reward_calculator.tenhou_log_to_mjai_log(tenhou_path, mjai_path, player_id=player_id)


def download_with_tensoul(url, tenhou_path, username=None, password=None):
    reward_calculator = _load_reward_calculator_module()

    asyncio.run(reward_calculator.download_majsoul_tenhou_log(url, tenhou_path, username=username, password=password))


def run_mortal_mapping(mjai_path, mapped_path, player_id, model_name=DEFAULT_MODEL_NAME):
    model_path = resolve_model_path(model_name)

    from mortal_runner import run_mortal_mapping as run_mapping

    run_mapping(ROOT, mjai_path, mapped_path, player_id, model_path=model_path)


def copy_outputs(tenhou_path, mjai_path, mapped_path, player_id=None):
    output_pairs = [
        (Path(mjai_path), CURRENT_DATA_DIR / "log.json"),
        (Path(mapped_path), CURRENT_DATA_DIR / "mortal-output-p2-mapped.jsonl"),
        (Path(tenhou_path), CURRENT_DATA_DIR / "majsoul-tenhou-current.json"),
    ]
    staged = []
    with CURRENT_OUTPUTS_LOCK:
        CURRENT_DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            for source, target in output_pairs:
                temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                staged.append((temporary, target))
                shutil.copyfile(source, temporary)
            if player_id is not None:
                target = CURRENT_DATA_DIR / "mortal-viewer-config.json"
                temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                staged.append((temporary, target))
                temporary.write_text(
                    json.dumps({"player_id": int(player_id)}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            for temporary, target in staged:
                temporary.replace(target)
            cleanup_legacy_current_outputs()
        finally:
            for temporary, _target in staged:
                if temporary.exists():
                    temporary.unlink()


def cleanup_legacy_current_outputs():
    for name in LEGACY_CURRENT_OUTPUT_NAMES:
        legacy_path = ROOT / name
        if legacy_path.exists() and legacy_path.is_file():
            legacy_path.unlink()


def write_history_metadata(job_id, work_dir, url, paipu, player_id, tenhou_path, mjai_path, mapped_path):
    metadata = {
        "job_id": job_id,
        "url": url,
        "paipu": paipu,
        "player_id": player_id,
        "created_at": time.time(),
        "files": {
            "tenhou": tenhou_path.name,
            "mjai": mjai_path.name,
            "mapped": mapped_path.name,
        },
    }
    metadata_path = work_dir / "metadata.json"
    temporary = metadata_path.with_name(f".{metadata_path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(metadata_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def resolve_player_id(source_path, requested_player_id, player_name="", paipu=""):
    if isinstance(requested_player_id, bool):
        raise ValueError("player_id 必须是 0-3")
    if str(requested_player_id) != "auto":
        player_id = int(requested_player_id)
        if player_id not in (0, 1, 2, 3):
            raise ValueError("player_id 必须是 0-3")
        return player_id

    source_data = json.loads(Path(source_path).read_text(encoding="utf-8-sig"))
    target_actor = source_data.get("_target_actor")
    if type(target_actor) is int and target_actor in (0, 1, 2, 3):
        return target_actor

    account_id = account_id_from_paipu(paipu)
    if account_id is not None:
        seat = player_seat_by_account_id(source_data, account_id)
        if seat is not None:
            return seat
        if tenhou_source_has_player_names(source_data):
            return 0

    target = str(player_name or "").strip()
    if not target:
        raise ValueError("无法从分享链接识别默认视角，请手动选择玩家 ID")

    names = player_names_from_source(source_data)
    lowered = target.casefold()
    for seat, name in enumerate(names):
        if str(name).strip().casefold() == lowered:
            return seat
    for seat, name in enumerate(names):
        if lowered in str(name).strip().casefold():
            return seat
    raise ValueError(f"没有在牌谱玩家里找到昵称：{target}")


def load_source_data(source):
    if isinstance(source, dict):
        return source
    return json.loads(Path(source).read_text(encoding="utf-8-sig"))


class UnsupportedThreePlayerError(ValueError):
    pass


def reject_unsupported_three_player(source):
    data = load_source_data(source)
    names = data.get("name")
    rating = str(data.get("ratingc") or "").strip().upper()
    rule = data.get("rule")
    display = str(rule.get("disp") or "") if isinstance(rule, dict) else ""
    if (
        rating == "PF3"
        or (isinstance(names, list) and len(names) == 3)
        or "三麻" in display
        or "3-Player" in display
    ):
        raise UnsupportedThreePlayerError(THREE_PLAYER_UNSUPPORTED_MESSAGE)


def tenhou_source_has_player_names(source):
    data = load_source_data(source)
    names = data.get("name") or []
    return len(names) >= 4


def player_seat_by_account_id(source, account_id):
    data = load_source_data(source)
    for account in data.get("Game", {}).get("accounts", []):
        if int(account.get("accountId", -1)) == int(account_id):
            return int(account.get("seat", 0))
    return None


def player_names_from_source(source):
    data = load_source_data(source)
    if "Game" in data:
        names = [str(i) for i in range(4)]
        for account in data["Game"].get("accounts", []):
            seat = int(account.get("seat", 0))
            if 0 <= seat < 4:
                names[seat] = str(account.get("nickname") or seat)
        return names
    names = data.get("name") or []
    if len(names) >= 4:
        return [str(name) for name in names[:4]]
    raise ValueError("这条牌谱里没有可用于自动识别的玩家名")


def completed_history_items(limit=5):
    limit = max(1, min(int(limit or 5), HISTORY_KEEP))
    items = []
    if not PAIPU_JOBS_DIR.exists():
        return items

    for work_dir in PAIPU_JOBS_DIR.iterdir():
        if not work_dir.is_dir():
            continue
        try:
            metadata = history_metadata_for_dir(work_dir)
            created_at = float(metadata.get("created_at") or work_dir.stat().st_mtime)
            if not math.isfinite(created_at) or created_at < 0:
                raise ValueError("历史复盘时间无效")
            created_at_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
        except (FileNotFoundError, OSError, OverflowError, TypeError, ValueError, json.JSONDecodeError):
            continue
        items.append({
            "job_id": metadata.get("job_id") or work_dir.name,
            "paipu": metadata.get("paipu") or work_dir.name,
            "player_id": metadata.get("player_id"),
            "created_at": created_at,
            "created_at_text": created_at_text,
        })

    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items[:limit]


def history_metadata_for_dir(work_dir):
    work_dir = Path(work_dir).resolve()
    metadata_path = work_dir / "metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    else:
        source_files = sorted(
            [*work_dir.glob("*.source.json"), *work_dir.glob("*.tenhou.json")],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not source_files:
            raise FileNotFoundError("这条历史复盘不完整")
        source_path = source_files[0]
        paipu = source_path.name.removesuffix(".source.json").removesuffix(".tenhou.json")
        metadata = {
            "job_id": work_dir.name,
            "paipu": paipu,
            "player_id": 2,
            "created_at": work_dir.stat().st_mtime,
            "files": {
                "tenhou": source_path.name,
                "mjai": "log.json",
                "mapped": "mortal-output-p2-mapped.jsonl",
            },
        }
    tenhou_path, mjai_path, mapped_path = history_paths_for_metadata(work_dir, metadata)
    if not all(path.exists() and path.is_file() and path.stat().st_size > 0 for path in (tenhou_path, mjai_path, mapped_path)):
        raise FileNotFoundError("这条历史复盘缺少结果文件")
    return metadata


def history_paths_for_metadata(work_dir, metadata):
    work_dir = Path(work_dir).resolve()
    if not isinstance(metadata, dict):
        raise ValueError("历史复盘元数据必须是 JSON 对象")
    files = metadata.get("files")
    if not isinstance(files, dict):
        raise ValueError("历史复盘元数据缺少 files")
    paths = []
    for key in ("tenhou", "mjai", "mapped"):
        name = files.get(key)
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ValueError("历史复盘文件名无效")
        path = (work_dir / name).resolve()
        if work_dir not in path.parents:
            raise ValueError("历史复盘文件必须位于任务目录")
        paths.append(path)
    return tuple(paths)


def restore_history(job_id):
    work_dir = (PAIPU_JOBS_DIR / safe_name(job_id)).resolve()
    if PAIPU_JOBS_DIR.resolve() not in work_dir.parents or not work_dir.is_dir():
        raise FileNotFoundError("找不到这条历史复盘")

    metadata = history_metadata_for_dir(work_dir)
    tenhou_path, mjai_path, mapped_path = history_paths_for_metadata(work_dir, metadata)
    copy_outputs(tenhou_path, mjai_path, mapped_path, metadata.get("player_id"))
    return metadata


def delete_history(job_id):
    work_dir = (PAIPU_JOBS_DIR / safe_name(job_id)).resolve()
    if PAIPU_JOBS_DIR.resolve() not in work_dir.parents or not work_dir.is_dir():
        raise FileNotFoundError("找不到这条历史复盘")
    try:
        metadata = history_metadata_for_dir(work_dir)
    except Exception:
        metadata = {"job_id": work_dir.name}
    shutil.rmtree(work_dir)
    return metadata


def cleanup_history(keep=HISTORY_KEEP):
    if not PAIPU_JOBS_DIR.exists():
        return
    work_dirs = [path for path in PAIPU_JOBS_DIR.iterdir() if path.is_dir()]
    work_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for old_dir in work_dirs[keep:]:
        shutil.rmtree(old_dir, ignore_errors=True)


def analyze_job(job_id, url, player_id, username=None, password=None, player_name="", fetch_method="remote", model_name=DEFAULT_MODEL_NAME):
    work_dir = PAIPU_JOBS_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    paipu = extract_paipu(url)
    prefix = safe_name(paipu)
    source_path = work_dir / f"{prefix}.source.json"
    mjai_path = work_dir / "log.json"
    mapped_path = work_dir / "mortal-output-p2-mapped.jsonl"
    viewer_tenhou_path = source_path

    set_job(job_id, status="running", step="提取 paipu", paipu=paipu, progress=10)

    try:
        if fetch_method == "remote":
            remote_steps = {
                "queued": ("远程牌谱服务排队中", 22),
                "fetching": ("远程牌谱服务正在获取牌谱", 30),
                "retry_wait": ("服务器正在恢复雀魂登录，任务会自动继续（可能需 1–4 分钟）", 28),
                "converting": ("远程牌谱服务正在转换牌谱", 40),
                "ready": ("远程牌谱已就绪", 45),
            }

            def update_remote_status(status):
                step, progress = remote_steps.get(status, ("等待远程牌谱服务", 20))
                set_job(job_id, step=step, progress=progress)

            set_job(job_id, step="提交到 ninklang.tech 牌谱服务", progress=20)
            download_with_remote_service(url, source_path, update_remote_status)
            player_id = resolve_player_id(source_path, player_id, player_name, paipu)
            set_job(job_id, player_id=player_id)
            set_job(job_id, step="转换为 mjai log.json", progress=50)
            convert_tenhou_to_mjai(source_path, mjai_path, player_id)
        elif fetch_method == "tensoul":
            set_job(job_id, step="使用 tensoul 账号密码获取", progress=20)
            download_with_tensoul(url, source_path, username=username, password=password)
            reject_unsupported_three_player(source_path)
            player_id = resolve_player_id(source_path, player_id, player_name, paipu)
            set_job(job_id, player_id=player_id)
            set_job(job_id, step="转换为 mjai log.json", progress=45)
            convert_tenhou_to_mjai(source_path, mjai_path, player_id)
        else:
            raise ValueError("fetch_method 必须是 remote 或 tensoul")

        set_job(job_id, step="运行 Mortal 分析", progress=70, model_name=model_name)
        run_mortal_mapping(mjai_path, mapped_path, player_id, model_name)

        set_job(job_id, step="写入现有复盘页读取的文件", progress=90)
        copy_outputs(viewer_tenhou_path, mjai_path, mapped_path, player_id)
        write_history_metadata(job_id, work_dir, url, paipu, player_id, viewer_tenhou_path, mjai_path, mapped_path)
        cleanup_history()

        set_job(
            job_id,
            status="done",
            step="完成",
            progress=100,
            viewer="/mortal-output-viewer.html",
            files={
                "tenhou": str(source_path),
                "mjai": str(mjai_path),
                "mapped": str(mapped_path),
            },
        )
    except Exception as exc:
        error_text = (
            str(exc)
            if isinstance(exc, UnsupportedThreePlayerError)
            else f"{type(exc).__name__}: {exc}"
        )
        traceback_text = traceback.format_exc()
        for secret in (username, password):
            if secret:
                error_text = error_text.replace(secret, "***")
                traceback_text = traceback_text.replace(secret, "***")
        set_job(
            job_id,
            status="error",
            step="失败",
            error=error_text,
            traceback=traceback_text,
        )
    finally:
        prune_finished_jobs()


class AnalysisScheduler:
    def __init__(self, target, max_pending=MAX_PENDING_JOBS):
        self._target = target
        self._queue = queue.Queue(maxsize=max_pending)
        self._start_lock = threading.Lock()
        self._worker = None

    def submit(self, *args, **kwargs):
        try:
            self._queue.put_nowait((args, kwargs))
        except queue.Full:
            return False
        with self._start_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run,
                    name="mortal-analysis-worker",
                    daemon=True,
                )
                self._worker.start()
        return True

    def _run(self):
        while True:
            args, kwargs = self._queue.get()
            try:
                self._target(*args, **kwargs)
            except Exception:
                traceback.print_exc()
            finally:
                self._queue.task_done()


ANALYSIS_SCHEDULER = AnalysisScheduler(analyze_job)


ANALYZER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>雀魂牌谱一键分析</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      color: #1d2428;
      background: linear-gradient(145deg, #102028, #1c2b31);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main {
      width: min(820px, 100%);
      background: #f7f4e8;
      border: 1px solid #d8ceb2;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 18px 48px rgba(0,0,0,.28);
    }
    header { padding: 18px 20px; background: #ebe4cf; border-bottom: 1px solid #d8ceb2; }
    h1 { margin: 0; font-size: 22px; }
    .sub { margin-top: 6px; color: #687276; font-size: 13px; }
    section { padding: 18px 20px 20px; display: grid; gap: 12px; }
    textarea, select, input {
      width: 100%;
      border: 1px solid #d8ceb2;
      border-radius: 6px;
      background: #fffdf7;
      padding: 11px;
      font: inherit;
    }
    textarea { min-height: 94px; resize: vertical; font-family: ui-monospace, Consolas, monospace; }
    label { display: grid; gap: 7px; font-weight: 800; font-size: 13px; }
    .grid { display: grid; grid-template-columns: 1fr 150px; gap: 10px; align-items: end; }
    .credentials-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; align-items: start; }
    .action-grid { display: grid; grid-template-columns: 1fr 1fr 1fr 150px; gap: 10px; align-items: end; }
    .password-field { position: relative; }
    .password-field input { padding-right: 72px; }
    .password-toggle {
      position: absolute;
      right: 6px;
      top: 6px;
      height: 28px;
      min-width: 56px;
      padding: 0 10px;
      border: 1px solid #d8ceb2;
      background: #ebe4cf;
      color: #1d2428;
      font-size: 12px;
    }
    .security-note {
      border: 1px solid #d8ceb2;
      border-radius: 6px;
      background: #fffdf7;
      padding: 9px 11px;
      color: #687276;
      font-size: 12px;
      line-height: 1.5;
      font-weight: 600;
    }
    button, a {
      height: 38px;
      border: 0;
      border-radius: 5px;
      background: #246f58;
      color: white;
      padding: 0 14px;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    button:disabled { opacity: .5; cursor: default; }
    a.secondary { color: #1d2428; background: #fffdf7; border: 1px solid #d8ceb2; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; }
    .history-panel {
      border: 1px solid #d8ceb2;
      border-radius: 6px;
      background: #fffdf7;
      padding: 10px;
      display: grid;
      gap: 8px;
    }
    .history-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font-weight: 800;
      font-size: 13px;
    }
    .history-head select { width: 96px; padding: 7px; }
    .history-list {
      display: grid;
      align-content: start;
      gap: 6px;
      height: 230px;
      overflow-y: scroll;
      padding-right: 16px;
      background:
        linear-gradient(#c9bb9b, #c9bb9b) right 4px top 10px / 6px calc(100% - 20px) no-repeat;
      scrollbar-gutter: stable;
      scrollbar-width: thin;
      scrollbar-color: #c4b896 #e6dece;
    }
    .history-list::-webkit-scrollbar {
      width: 16px;
    }
    .history-list::-webkit-scrollbar-track {
      background: #e6dece;
      border: 1px solid #d8ceb2;
      border-radius: 99px;
    }
    .history-list::-webkit-scrollbar-thumb {
      min-height: 42px;
      background: #c4b896;
      border: 3px solid #e6dece;
      border-radius: 99px;
    }
    .history-list::-webkit-scrollbar-thumb:hover {
      background: #b0a47e;
    }
    .history-empty { color: #687276; font-size: 12px; }
    .history-item {
      width: 100%;
      height: auto;
      min-height: 42px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
      text-align: left;
      color: #1d2428;
      background: #f7f4e8;
      border: 1px solid #d8ceb2;
      padding: 8px 10px;
      font-weight: 700;
    }
    .history-actions { display: flex; gap: 6px; align-items: center; }
    .history-action {
      height: 30px;
      min-width: 46px;
      padding: 0 9px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 800;
    }
    .history-open {
      background: #e7f2ec;
      color: #246f58;
      border-color: #a9c9ba;
    }
    .history-delete {
      background: #fff4f0;
      color: #9a3120;
      border-color: #e0b4a8;
    }
    .history-item small {
      display: block;
      margin-top: 3px;
      color: #687276;
      font-weight: 600;
    }
    .status {
      min-height: 62px;
      border: 1px solid #d8ceb2;
      border-radius: 6px;
      padding: 10px;
      background: #fffdf7;
      line-height: 1.55;
      font-size: 13px;
    }
    .bar { height: 12px; border-radius: 99px; background: #ddd6c3; overflow: hidden; }
    .fill { height: 100%; width: 0%; background: linear-gradient(90deg, #246f58, #41c48d); transition: width .2s; }
    pre { max-height: 180px; overflow: auto; margin: 0; padding: 10px; background: #fffdf7; border: 1px solid #d8ceb2; border-radius: 6px; font-size: 12px; }
    @media (max-width: 680px) {
      .grid, .credentials-grid, .action-grid { grid-template-columns: 1fr; }
      button, a { width: 100%; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>雀魂牌谱一键分析</h1>
      <div class="sub">粘贴雀魂分享链接，选择 ninklang.tech 在线获取或 tensoul 本地账号密码获取，然后自动打开现有复盘页。</div>
    </header>
    <section>
      <label>
        雀魂分享链接
        <textarea id="url" placeholder="https://game.maj-soul.com/1/?paipu=260219-xxxx_xxxx"></textarea>
      </label>
      <div class="credentials-grid">
        <label>
          雀魂账号 / 邮箱
          <input id="username" autocomplete="username" placeholder="可留空，仅用于 tensoul 回退">
        </label>
        <label>
          雀魂密码
          <span class="password-field">
            <input id="password" type="password" autocomplete="current-password" placeholder="可留空，仅用于 tensoul 回退">
            <button class="password-toggle" id="toggle-password" type="button">显示</button>
          </span>
        </label>
      </div>
      <div class="security-note">ninklang.tech 在线获取只会向你的牌谱服务提交分享链接，不会上传雀魂账号密码；tensoul 会使用账号密码在本机获取牌谱，这里会挤号。两种方式成功后都会写入本地历史。</div>
      <div class="action-grid">
        <label>
          获取方式
          <select id="fetch-mode">
            <option value="remote" selected>ninklang.tech 在线获取（推荐）</option>
            <option value="tensoul">tensoul 本地账号密码获取（仅四麻）</option>
          </select>
        </label>
        <label>
          Mortal 模型
          <select id="model-name">
            <option value="mortal.pth" selected>mortal.pth</option>
          </select>
        </label>
        <label>
          分析玩家 ID
          <select id="player">
            <option value="auto" selected>自动识别（自家视角）</option>
            <option value="0">0</option>
            <option value="1">1</option>
            <option value="2">2</option>
            <option value="3">3</option>
          </select>
        </label>
        <button id="start">开始分析</button>
      </div>
      <div class="bar"><div class="fill" id="fill"></div></div>
      <div class="status" id="status">等待输入链接。</div>
      <div class="row">
        <a class="secondary" href="/mortal-output-viewer.html" target="_blank">打开复盘页</a>
      </div>
      <div class="history-panel">
        <div class="history-head">
          <span>历史复盘</span>
          <select id="history-limit">
            <option value="20">最近 20 次</option>
            <option value="5" selected>最近 5 次</option>
            <option value="10">最近 10 次</option>
          </select>
        </div>
        <div class="history-list" id="history-list">
          <div class="history-empty">正在读取历史记录...</div>
        </div>
      </div>
      <pre id="detail">进度详情会显示在这里。</pre>
    </section>
  </main>
  <script>
    const startBtn = document.getElementById('start');
    const statusBox = document.getElementById('status');
    const fill = document.getElementById('fill');
    const detail = document.getElementById('detail');
    const historyLimit = document.getElementById('history-limit');
    const historyList = document.getElementById('history-list');
    const playerSelect = document.getElementById('player');
    const fetchModeSelect = document.getElementById('fetch-mode');
    const modelSelect = document.getElementById('model-name');
    const passwordInput = document.getElementById('password');
    const togglePasswordBtn = document.getElementById('toggle-password');

    togglePasswordBtn.onclick = () => {
      const shouldShow = passwordInput.type === 'password';
      passwordInput.type = shouldShow ? 'text' : 'password';
      togglePasswordBtn.textContent = shouldShow ? '隐藏' : '显示';
      togglePasswordBtn.setAttribute('aria-label', shouldShow ? '隐藏密码' : '显示密码');
    };

    function setProgress(job) {
      fill.style.width = `${job.progress || 0}%`;
      statusBox.textContent = `${job.step || '处理中'} (${job.progress || 0}%)`;
      detail.textContent = JSON.stringify(job, null, 2);
    }

    function shortPaipu(paipu) {
      if (!paipu) return '未知牌谱';
      return paipu.length > 34 ? `${paipu.slice(0, 30)}...` : paipu;
    }

    function showHistoryMessage(message) {
      historyList.replaceChildren();
      const empty = document.createElement('div');
      empty.className = 'history-empty';
      empty.textContent = message;
      historyList.appendChild(empty);
    }

    function updateFetchModeUi() {
      const tensoulMode = fetchModeSelect.value === 'tensoul';
      startBtn.textContent = '开始分析';
      document.getElementById('username').disabled = !tensoulMode;
      passwordInput.disabled = !tensoulMode;
      togglePasswordBtn.disabled = !tensoulMode;
      statusBox.textContent = tensoulMode
        ? 'tensoul 模式会使用账号密码在本地获取牌谱，仅支持四麻东风战或半庄。'
        : '在线模式通过 ninklang.tech 获取牌谱，不需要输入雀魂账号密码。';
    }

    async function loadModels() {
      try {
        const res = await fetch('/api/models', { cache: 'no-store' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        const models = data.models?.length ? data.models : [data.default || 'mortal.pth'];
        modelSelect.innerHTML = '';
        for (const name of models) {
          const option = document.createElement('option');
          option.value = name;
          option.textContent = name;
          option.selected = name === (data.default || 'mortal.pth');
          modelSelect.appendChild(option);
        }
      } catch (error) {
        modelSelect.innerHTML = '<option value="mortal.pth">mortal.pth</option>';
        statusBox.textContent = `读取模型列表失败：${error.message}`;
      }
    }

    async function loadHistory() {
      const limit = historyLimit.value || '5';
      try {
        const res = await fetch(`/api/history?limit=${encodeURIComponent(limit)}`, { cache: 'no-store' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        if (!data.items.length) {
          showHistoryMessage('还没有成功分析过的历史复盘。');
          return;
        }
        historyList.replaceChildren();
        for (const item of data.items) {
          const row = document.createElement('div');
          row.className = 'history-item';
          const summary = document.createElement('span');
          summary.appendChild(document.createTextNode(shortPaipu(item.paipu)));
          const details = document.createElement('small');
          details.textContent = `${item.created_at_text} · 玩家 ${item.player_id}`;
          summary.appendChild(details);
          const actions = document.createElement('span');
          actions.className = 'history-actions';
          const openButton = document.createElement('button');
          openButton.className = 'history-action history-open';
          openButton.type = 'button';
          openButton.textContent = '打开';
          openButton.onclick = () => openHistory(item.job_id);
          const deleteButton = document.createElement('button');
          deleteButton.className = 'history-action history-delete';
          deleteButton.type = 'button';
          deleteButton.textContent = '删除';
          deleteButton.onclick = () => deleteHistory(item.job_id);
          actions.append(openButton, deleteButton);
          row.append(summary, actions);
          historyList.appendChild(row);
        }
      } catch (error) {
        showHistoryMessage(error.message);
      }
    }

    async function openHistory(jobId) {
      statusBox.textContent = '正在切换到历史复盘...';
      const res = await fetch('/api/use-history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: jobId }),
      });
      const data = await res.json();
      if (!res.ok) {
        statusBox.textContent = data.error || `HTTP ${res.status}`;
        return;
      }
      window.location.href = data.viewer || '/mortal-output-viewer.html';
    }

    async function deleteHistory(jobId) {
      if (!confirm('删除这条历史复盘？')) return;
      const res = await fetch('/api/delete-history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: jobId }),
      });
      const data = await res.json();
      if (!res.ok) {
        statusBox.textContent = data.error || `HTTP ${res.status}`;
        return;
      }
      statusBox.textContent = '已删除这条历史复盘。';
      loadHistory();
    }

    async function poll(jobId, failures = 0) {
      try {
        const res = await fetch(`/api/status?job_id=${encodeURIComponent(jobId)}`, { cache: 'no-store' });
        const job = await res.json();
        if (!res.ok) throw new Error(job.error || `HTTP ${res.status}`);
        setProgress(job);
        if (job.status === 'done') {
          statusBox.textContent = '分析完成，正在打开复盘页。';
          loadHistory();
          window.location.href = job.viewer || '/mortal-output-viewer.html';
          return;
        }
        if (job.status === 'error') {
          statusBox.textContent = job.error || '分析失败';
          startBtn.disabled = false;
          return;
        }
        setTimeout(() => poll(jobId, 0), 1000);
      } catch (error) {
        const nextFailures = failures + 1;
        if (nextFailures <= 5) {
          const delay = Math.min(1000 * (2 ** (nextFailures - 1)), 5000);
          statusBox.textContent = `状态连接暂时中断，${Math.ceil(delay / 1000)} 秒后重试...`;
          detail.textContent = String(error.stack || error);
          setTimeout(() => poll(jobId, nextFailures), delay);
          return;
        }
        statusBox.textContent = `无法读取任务状态：${error.message}`;
        detail.textContent = String(error.stack || error);
        startBtn.disabled = false;
      }
    }

    startBtn.onclick = async () => {
      startBtn.disabled = true;
      fill.style.width = '0%';
      detail.textContent = '';
      try {
        statusBox.textContent = '提交任务中...';
        const res = await fetch('/api/analyze', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url: document.getElementById('url').value,
            fetch_method: fetchModeSelect.value,
            model_name: modelSelect.value,
            player_id: playerSelect.value,
            username: document.getElementById('username').value,
            password: document.getElementById('password').value,
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        passwordInput.value = '';
        poll(data.job_id, 0);
        loadHistory();
      } catch (error) {
        statusBox.textContent = error.message;
        detail.textContent = String(error.stack || error);
        startBtn.disabled = false;
      }
    };

    // Drag-to-scroll for history list
    (function() {
      const list = historyList;
      let dragging = false;
      let pending = false;
      let startY = 0;
      let startScroll = 0;
      const THRESHOLD = 5;

      list.addEventListener('mousedown', function(e) {
        if (e.button !== 0) return;
        pending = true;
        dragging = false;
        startY = e.clientY;
        startScroll = list.scrollTop;
      });

      document.addEventListener('mousemove', function(e) {
        if (!pending && !dragging) return;
        const delta = e.clientY - startY;
        if (pending && Math.abs(delta) < THRESHOLD) return;
        if (pending) {
          pending = false;
          dragging = true;
          list.style.cursor = 'grabbing';
          list.style.userSelect = 'none';
        }
        list.scrollTop = startScroll + delta;
        e.preventDefault();
      });

      document.addEventListener('mouseup', function() {
        if (dragging) {
          list.style.cursor = '';
          list.style.userSelect = '';
        }
        pending = false;
        dragging = false;
      });
    })();

    historyLimit.onchange = loadHistory;
    fetchModeSelect.onchange = updateFetchModeUi;
    updateFetchModeUi();
    loadModels();
    loadHistory();
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    timeout = 10
    protocol_version = "HTTP/1.1"

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, content_type="text/html; charset=utf-8", status=HTTPStatus.OK):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        if self.headers.get_content_type().casefold() != "application/json":
            self.close_connection = True
            raise HttpRequestError("Content-Type 必须是 application/json", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.close_connection = True
            raise HttpRequestError("缺少 Content-Length", HTTPStatus.LENGTH_REQUIRED)
        try:
            length = int(raw_length)
        except ValueError as exc:
            self.close_connection = True
            raise HttpRequestError("Content-Length 无效") from exc
        if length < 0:
            self.close_connection = True
            raise HttpRequestError("Content-Length 无效")
        if length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            raise HttpRequestError("请求体过大", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise HttpRequestError("请求体不完整")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpRequestError("请求体必须是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise HttpRequestError("请求体必须是 JSON 对象")
        return payload

    def send_exception(self, exc):
        status = exc.status if isinstance(exc, HttpRequestError) else HTTPStatus.BAD_REQUEST
        self.send_json({"error": str(exc)}, status)

    def request_host_is_allowed(self):
        try:
            parsed = urllib.parse.urlsplit("//" + self.headers.get("Host", ""))
            host = (parsed.hostname or "").casefold().rstrip(".")
            port = parsed.port
        except ValueError:
            return False
        local_host, local_port = self.connection.getsockname()[:2]
        allowed_hosts = {"localhost", "127.0.0.1", "::1", str(local_host).casefold().split("%", 1)[0]}
        return host in allowed_hosts and (port is None or port == local_port)

    def request_origin_is_allowed(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed_origin = urllib.parse.urlsplit(origin)
            parsed_host = urllib.parse.urlsplit("//" + self.headers.get("Host", ""))
            origin_host = (parsed_origin.hostname or "").casefold().rstrip(".")
            request_host = (parsed_host.hostname or "").casefold().rstrip(".")
            local_port = self.connection.getsockname()[1]
            origin_port = parsed_origin.port or 80
            request_port = parsed_host.port or local_port
        except ValueError:
            return False
        return parsed_origin.scheme == "http" and origin_host == request_host and origin_port == request_port

    def reject_untrusted_request(self, include_origin=False):
        if not self.request_host_is_allowed() or (include_origin and not self.request_origin_is_allowed()):
            if include_origin:
                self.close_connection = True
            self.send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return True
        return False

    def do_GET(self):
        if self.reject_untrusted_request():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/paipu-analyzer.html"):
            self.send_text(ANALYZER_HTML)
            return
        if parsed.path == "/api/status":
            qs = urllib.parse.parse_qs(parsed.query)
            job_id = qs.get("job_id", [""])[0]
            with JOBS_LOCK:
                stored_job = JOBS.get(job_id)
                job = stored_job.copy() if stored_job else None
            if not job:
                self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json(job)
            return
        if parsed.path == "/api/history":
            try:
                qs = urllib.parse.parse_qs(parsed.query)
                limit = int(qs.get("limit", ["5"])[0])
                items = completed_history_items(limit)
            except (TypeError, ValueError):
                self.send_exception(HttpRequestError("limit 必须是整数"))
                return
            self.send_json({"items": items})
            return
        if parsed.path == "/api/models":
            self.send_json({"models": available_model_names(), "default": DEFAULT_MODEL_NAME})
            return

        if static_path_is_private(parsed.path):
            self.send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        if not static_path_is_public(parsed.path):
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        root = ROOT.resolve()
        file_path = (root / parsed.path.lstrip("/")).resolve()
        if root not in file_path.parents and file_path != root:
            self.send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        size = file_path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        parts = tuple(part.casefold() for part in Path(parsed.path.lstrip("/")).parts)
        if len(parts) >= 2 and parts[:2] == ("log-viewer", "files"):
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with file_path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, length=64 * 1024)

    def do_POST(self):
        if self.reject_untrusted_request(include_origin=True):
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/use-history":
            try:
                payload = self.read_json_body()
                metadata = restore_history(str(payload.get("job_id") or ""))
            except Exception as exc:
                self.send_exception(exc)
                return
            self.send_json({"viewer": "/mortal-output-viewer.html", "history": metadata})
            return

        if parsed.path == "/api/delete-history":
            try:
                payload = self.read_json_body()
                metadata = delete_history(str(payload.get("job_id") or ""))
            except Exception as exc:
                self.send_exception(exc)
                return
            self.send_json({"deleted": True, "history": metadata})
            return

        if parsed.path != "/api/analyze":
            self.close_connection = True
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self.read_json_body()
            url = str(payload.get("url") or "")
            player_id = payload.get("player_id", "auto")
            player_name = str(payload.get("player_name") or "")
            fetch_method = str(payload.get("fetch_method") or "remote")
            model_name = str(payload.get("model_name") or DEFAULT_MODEL_NAME)
            username = str(payload.get("username") or "")
            password = str(payload.get("password") or "")
            if fetch_method not in ("remote", "tensoul"):
                raise ValueError("fetch_method 必须是 remote 或 tensoul")
            resolve_model_path(model_name)
            if isinstance(player_id, bool):
                raise ValueError("player_id 必须是 0-3")
            if str(player_id) != "auto":
                player_id = int(player_id)
                if player_id not in (0, 1, 2, 3):
                    raise ValueError("player_id 必须是 0-3")
            extract_paipu(url)
        except Exception as exc:
            self.send_exception(exc)
            return

        job_id = uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "step": "排队中",
                "progress": 0,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        submitted = ANALYSIS_SCHEDULER.submit(
            job_id,
            url,
            player_id,
            username,
            password,
            player_name,
            fetch_method,
            model_name,
        )
        if not submitted:
            with JOBS_LOCK:
                JOBS.pop(job_id, None)
            self.send_json({"error": "等待分析的任务过多，请稍后重试。"}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        self.send_json({"job_id": job_id})

    def log_message(self, fmt, *args):
        if fmt.startswith("Request timed out:"):
            return
        if len(args) >= 2:
            request_line = str(args[0])
            try:
                status = int(args[1])
            except (TypeError, ValueError):
                status = 0
            if request_line.startswith("GET ") and 200 <= status < 400:
                return
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    parser = argparse.ArgumentParser(description="Local Mahjong Soul paipu analyzer server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"open http://{args.host}:{args.port}/paipu-analyzer.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("server stopped", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
