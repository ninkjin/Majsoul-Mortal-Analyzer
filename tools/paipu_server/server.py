import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
import threading
import time
import traceback
import shutil
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
PAIPU_JOBS_DIR = ROOT / "tmp" / "paipu_jobs"
CURRENT_DATA_DIR = ROOT / "viewer-data"
MODEL_DIR = ROOT / "mj_model"
DEFAULT_MODEL_NAME = "mortal.pth"
LEGACY_CURRENT_OUTPUT_NAMES = (
    "log.json",
    "mortal-output-p2-mapped.jsonl",
    "mortal-output-p2.jsonl",
    "majsoul-tenhou-current.json",
    "mortal-viewer-config.json",
)
HISTORY_KEEP = 20
JOBS = {}
JOBS_LOCK = threading.Lock()
_REWARD_CALCULATOR_MODULE = None


def extract_paipu(value):
    match = re.search(r"[?&]paipu=([^&#\s]+)", value or "") or re.search(r"^paipu=([^&#\s]+)", value or "")
    if not match:
        raise ValueError("没有找到 paipu 参数")
    return urllib.parse.unquote(match.group(1))


def safe_name(value):
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value).strip("_") or "paipu"


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


def public_record_replay(paipu, out_path):
    url = f"https://game.maj-soul.com/1/api/account/record_replay?paipu={urllib.parse.quote(paipu)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mortal-paipu-server/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            raw = res.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"公开接口请求失败：HTTP {exc.code}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("公开接口返回的不是合法 JSON") from exc

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def download_with_majgg(paipu, source_path, mjai_path, tenhou_summary_path=None):
    from majgg_fetcher import fetch_majgg_game, majgg_game_to_mjai_file, majgg_game_to_tenhou_summary_file

    record_uuid = paipu.split("_", 1)[0]
    if not record_uuid:
        raise ValueError("empty Mahjong Soul record uuid")
    data = fetch_majgg_game(record_uuid, source_path)
    majgg_game_to_mjai_file(data, mjai_path)
    if tenhou_summary_path:
        majgg_game_to_tenhou_summary_file(data, tenhou_summary_path)
    return source_path


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
    CURRENT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (CURRENT_DATA_DIR / "log.json").write_bytes(mjai_path.read_bytes())
    (CURRENT_DATA_DIR / "mortal-output-p2-mapped.jsonl").write_bytes(mapped_path.read_bytes())
    (CURRENT_DATA_DIR / "majsoul-tenhou-current.json").write_bytes(tenhou_path.read_bytes())
    if player_id is not None:
        (CURRENT_DATA_DIR / "mortal-viewer-config.json").write_text(
            json.dumps({"player_id": int(player_id)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    cleanup_legacy_current_outputs()


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
    (work_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def resolve_player_id(source_path, requested_player_id, player_name="", paipu=""):
    if str(requested_player_id) != "auto":
        player_id = int(requested_player_id)
        if player_id not in (0, 1, 2, 3):
            raise ValueError("player_id 必须是 0-3")
        return player_id

    account_id = account_id_from_paipu(paipu)
    if account_id is not None:
        seat = player_seat_by_account_id(source_path, account_id)
        if seat is not None:
            return seat
        if tenhou_source_has_player_names(source_path):
            return 0

    target = str(player_name or "").strip()
    if not target:
        raise ValueError("无法从分享链接识别默认视角，请手动选择玩家 ID")

    names = player_names_from_source(source_path)
    lowered = target.casefold()
    for seat, name in enumerate(names):
        if str(name).strip().casefold() == lowered:
            return seat
    for seat, name in enumerate(names):
        if lowered in str(name).strip().casefold():
            return seat
    raise ValueError(f"没有在牌谱玩家里找到昵称：{target}")


def tenhou_source_has_player_names(source_path):
    data = json.loads(Path(source_path).read_text(encoding="utf-8-sig"))
    names = data.get("name") or []
    return len(names) >= 4


def player_seat_by_account_id(source_path, account_id):
    data = json.loads(Path(source_path).read_text(encoding="utf-8-sig"))
    for account in data.get("Game", {}).get("accounts", []):
        if int(account.get("accountId", -1)) == int(account_id):
            return int(account.get("seat", 0))
    return None


def player_names_from_source(source_path):
    data = json.loads(Path(source_path).read_text(encoding="utf-8-sig"))
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
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        created_at = float(metadata.get("created_at") or work_dir.stat().st_mtime)
        items.append({
            "job_id": metadata.get("job_id") or work_dir.name,
            "paipu": metadata.get("paipu") or work_dir.name,
            "player_id": metadata.get("player_id"),
            "created_at": created_at,
            "created_at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at)),
        })

    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items[:limit]


def history_metadata_for_dir(work_dir):
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
    files = metadata.get("files", {})
    tenhou_path = work_dir / files.get("tenhou", "")
    mjai_path = work_dir / files.get("mjai", "")
    mapped_path = work_dir / files.get("mapped", "")
    if not all(path.exists() and path.is_file() and path.stat().st_size > 0 for path in (tenhou_path, mjai_path, mapped_path)):
        raise FileNotFoundError("这条历史复盘缺少结果文件")
    return metadata


def restore_history(job_id):
    work_dir = (PAIPU_JOBS_DIR / safe_name(job_id)).resolve()
    if PAIPU_JOBS_DIR.resolve() not in work_dir.parents or not work_dir.is_dir():
        raise FileNotFoundError("找不到这条历史复盘")

    metadata = history_metadata_for_dir(work_dir)
    files = metadata.get("files", {})
    tenhou_path = work_dir / files.get("tenhou", "")
    mjai_path = work_dir / files.get("mjai", "")
    mapped_path = work_dir / files.get("mapped", "")
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


def analyze_job(job_id, url, player_id, username=None, password=None, player_name="", fetch_method="majgg", model_name=DEFAULT_MODEL_NAME):
    work_dir = PAIPU_JOBS_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    paipu = extract_paipu(url)
    prefix = safe_name(paipu)
    source_path = work_dir / f"{prefix}.source.json"
    tenhou_summary_path = work_dir / f"{prefix}.tenhou-summary.json"
    mjai_path = work_dir / "log.json"
    mapped_path = work_dir / "mortal-output-p2-mapped.jsonl"
    viewer_tenhou_path = source_path

    set_job(job_id, status="running", step="提取 paipu", paipu=paipu, progress=10)

    try:
        if fetch_method == "majgg":
            set_job(job_id, step="使用 maj.gg 免登录获取", progress=20)
            download_with_majgg(paipu, source_path, mjai_path, tenhou_summary_path)
            viewer_tenhou_path = tenhou_summary_path
            player_id = resolve_player_id(source_path, player_id, player_name, paipu)
            set_job(job_id, player_id=player_id)
        elif fetch_method == "tensoul":
            set_job(job_id, step="使用 tensoul 账号密码获取", progress=20)
            download_with_tensoul(url, source_path, username=username, password=password)
            player_id = resolve_player_id(source_path, player_id, player_name, paipu)
            set_job(job_id, player_id=player_id)
            set_job(job_id, step="转换为 mjai log.json", progress=45)
            convert_tenhou_to_mjai(source_path, mjai_path, player_id)
        else:
            raise ValueError("fetch_method 必须是 majgg 或 tensoul")

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
    except BaseException as exc:
        error_text = f"{type(exc).__name__}: {exc}"
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
      <div class="sub">粘贴雀魂分享链接，选择 maj.gg 免登录获取或 tensoul 账号密码获取，然后自动打开现有复盘页。</div>
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
      <div class="security-note">maj.gg 免登录不需要账号密码；tensoul 会使用账号密码在本地获取牌谱，这里会挤号，但是不会泄露你的账号密码，因为全部都是在本地进行。两种方式成功后都会写入本地历史。</div>
      <div class="action-grid">
        <label>
          获取方式
          <select id="fetch-mode">
            <option value="majgg" selected>maj.gg 免登录获取</option>
            <option value="tensoul">tensoul 账号密码获取</option>
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

    function updateFetchModeUi() {
      const tensoulMode = fetchModeSelect.value === 'tensoul';
      startBtn.textContent = '开始分析';
      document.getElementById('username').disabled = !tensoulMode;
      passwordInput.disabled = !tensoulMode;
      togglePasswordBtn.disabled = !tensoulMode;
      statusBox.textContent = tensoulMode
        ? 'tensoul 模式会使用账号密码在本地获取牌谱。'
        : 'maj.gg 免登录模式不需要账号密码。';
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
          historyList.innerHTML = '<div class="history-empty">还没有成功分析过的历史复盘。</div>';
          return;
        }
        historyList.innerHTML = '';
        for (const item of data.items) {
          const row = document.createElement('div');
          row.className = 'history-item';
          row.innerHTML = `
            <span>${shortPaipu(item.paipu)}<small>${item.created_at_text} · 玩家 ${item.player_id}</small></span>
            <span class="history-actions">
              <button class="history-action history-open" type="button">打开</button>
              <button class="history-action history-delete" type="button">删除</button>
            </span>
          `;
          row.querySelector('.history-open').onclick = () => openHistory(item.job_id);
          row.querySelector('.history-delete').onclick = () => deleteHistory(item.job_id);
          historyList.appendChild(row);
        }
      } catch (error) {
        historyList.innerHTML = `<div class="history-empty">${error.message}</div>`;
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

    async function poll(jobId) {
      const res = await fetch(`/api/status?job_id=${encodeURIComponent(jobId)}`, { cache: 'no-store' });
      const job = await res.json();
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
      setTimeout(() => poll(jobId), 1000);
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
        poll(data.job_id);
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
    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, content_type="text/html; charset=utf-8", status=HTTPStatus.OK):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/paipu-analyzer.html"):
            self.send_text(ANALYZER_HTML)
            return
        if parsed.path == "/api/status":
            qs = urllib.parse.parse_qs(parsed.query)
            job_id = qs.get("job_id", [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self.send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json(job)
            return
        if parsed.path == "/api/history":
            qs = urllib.parse.parse_qs(parsed.query)
            limit = int(qs.get("limit", ["5"])[0])
            self.send_json({"items": completed_history_items(limit)})
            return
        if parsed.path == "/api/models":
            self.send_json({"models": available_model_names(), "default": DEFAULT_MODEL_NAME})
            return

        file_path = (ROOT / parsed.path.lstrip("/")).resolve()
        if ROOT not in file_path.parents and file_path != ROOT:
            self.send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8" if file_path.suffix == ".html" else "application/octet-stream"
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/use-history":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                metadata = restore_history(str(payload.get("job_id") or ""))
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"viewer": "/mortal-output-viewer.html", "history": metadata})
            return

        if parsed.path == "/api/delete-history":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                metadata = delete_history(str(payload.get("job_id") or ""))
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"deleted": True, "history": metadata})
            return

        if parsed.path != "/api/analyze":
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            url = str(payload.get("url") or "")
            player_id = payload.get("player_id", "auto")
            player_name = str(payload.get("player_name") or "")
            fetch_method = str(payload.get("fetch_method") or "majgg")
            model_name = str(payload.get("model_name") or DEFAULT_MODEL_NAME)
            username = str(payload.get("username") or "")
            password = str(payload.get("password") or "")
            if fetch_method not in ("majgg", "tensoul"):
                raise ValueError("fetch_method 必须是 majgg 或 tensoul")
            resolve_model_path(model_name)
            if str(player_id) != "auto":
                player_id = int(player_id)
                if player_id not in (0, 1, 2, 3):
                    raise ValueError("player_id 必须是 0-3")
            extract_paipu(url)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
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
        thread = threading.Thread(
            target=analyze_job,
            args=(job_id, url, player_id, username, password, player_name, fetch_method, model_name),
            daemon=True,
        )
        thread.start()
        self.send_json({"job_id": job_id})

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    parser = argparse.ArgumentParser(description="Local Mahjong Soul paipu analyzer server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"open http://{args.host}:{args.port}/paipu-analyzer.html", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
