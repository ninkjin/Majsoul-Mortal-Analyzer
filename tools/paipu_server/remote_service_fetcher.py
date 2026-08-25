import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVICE_URL = "https://ninklang.tech"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "paipu-service.local.json"
MAX_METADATA_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024
PENDING_STATES = {"queued", "fetching", "retry_wait", "converting"}
NETWORK_RETRY_DELAYS = (1, 2, 5)
RETRYABLE_HTTP_STATUSES = {408, 425, 429}
DEFAULT_REQUEST_TIMEOUT = 20
MAX_RETRY_AFTER_SECONDS = 30
MAX_ERROR_TEXT_CHARS = 300
WAIT_TIMEOUT_MESSAGE = "等待远程牌谱服务超时，请稍后重试。"


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_REMOTE_OPENER = urllib.request.build_opener(NoRedirectHandler())


class RemoteServiceError(RuntimeError):
    """A safe, user-facing error from the remote paipu service."""


class RemoteServiceConnectionError(RemoteServiceError):
    """A temporary network failure that can be retried safely."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class RemoteServiceConfig:
    service_url: str
    api_key: str


def _config_path():
    configured = os.environ.get("MORTAL_PAIPU_CONFIG", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG_PATH


def _normalized_service_url(value):
    url = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RemoteServiceError("远程牌谱服务地址无效。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RemoteServiceError("远程牌谱服务地址不能包含账号、查询参数或片段。")
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RemoteServiceError("远程牌谱服务必须使用 HTTPS。")
    return url


def load_remote_service_config():
    file_config = {}
    config_path = _config_path()
    if config_path.is_file():
        try:
            file_config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RemoteServiceError(f"无法读取远程牌谱服务配置：{config_path.name}") from exc
        if not isinstance(file_config, dict):
            raise RemoteServiceError(f"远程牌谱服务配置必须是 JSON 对象：{config_path.name}")

    service_url = os.environ.get("MORTAL_PAIPU_SERVICE_URL", "").strip()
    service_url = service_url or str(file_config.get("service_url") or DEFAULT_SERVICE_URL)
    api_key = os.environ.get("MORTAL_PAIPU_API_KEY", "").strip()
    api_key = api_key or str(file_config.get("api_key") or "").strip()
    if not api_key or not api_key.startswith("pk_") or len(api_key) < 20:
        raise RemoteServiceError(
            "远程牌谱服务尚未配置 API Key。请设置 MORTAL_PAIPU_API_KEY，"
            "或在项目目录创建 paipu-service.local.json。"
        )
    return RemoteServiceConfig(_normalized_service_url(service_url), api_key)


def _read_limited(response, limit):
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise RemoteServiceError("远程牌谱服务返回的数据过大，已拒绝处理。")
    return payload


def _retry_after_seconds(headers):
    try:
        value = float(headers.get("Retry-After", ""))
    except (AttributeError, TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, MAX_RETRY_AFTER_SECONDS)


def _service_text(value, limit=MAX_ERROR_TEXT_CHARS):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _open_url(request, timeout):
    return _REMOTE_OPENER.open(request, timeout=timeout)


def _safe_error_message(status, payload):
    if status == 401:
        return "远程牌谱服务 API Key 无效或已被撤销。"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = _service_text(error.get("code"), 80)
        message = _service_text(error.get("message"))
        if code and message:
            return f"远程牌谱服务返回 {code}：{message}"
        if code:
            return f"远程牌谱服务返回错误码：{code}"
        if message:
            return f"远程牌谱服务请求失败：{message}"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail:
        return f"远程牌谱服务请求失败（HTTP {status}）：{_service_text(detail)}"
    return f"远程牌谱服务请求失败：HTTP {status}"


def _request_json(
    url,
    api_key,
    *,
    method="GET",
    body=None,
    timeout=DEFAULT_REQUEST_TIMEOUT,
    max_bytes=MAX_METADATA_BYTES,
    extra_headers=None,
):
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mortal-paipu-analyzer/2.0",
    }
    if extra_headers:
        headers.update(extra_headers)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _open_url(request, timeout) as response:
            raw = _read_limited(response, max_bytes)
    except urllib.error.HTTPError as exc:
        try:
            error_raw = _read_limited(exc, MAX_METADATA_BYTES)
            error_payload = json.loads(error_raw.decode("utf-8"))
        except (RemoteServiceError, UnicodeDecodeError, json.JSONDecodeError):
            error_payload = {}
        message = _safe_error_message(exc.code, error_payload)
        if exc.code in RETRYABLE_HTTP_STATUSES or exc.code >= 500:
            raise RemoteServiceConnectionError(message, _retry_after_seconds(exc.headers)) from None
        raise RemoteServiceError(message) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None)
        reason_text = str(reason or exc)
        if len(reason_text) > 120:
            reason_text = reason_text[:117] + "..."
        raise RemoteServiceConnectionError(f"无法连接远程牌谱服务：{reason_text}") from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteServiceError("远程牌谱服务返回的不是合法 JSON。") from exc
    if not isinstance(payload, dict):
        raise RemoteServiceError("远程牌谱服务返回的数据格式不正确。")
    return payload


def _request_json_with_retries(
    url,
    api_key,
    *,
    sleep=time.sleep,
    clock=time.monotonic,
    deadline=None,
    **kwargs,
):
    request_timeout = float(kwargs.get("timeout", DEFAULT_REQUEST_TIMEOUT))
    for attempt in range(len(NETWORK_RETRY_DELAYS) + 1):
        request_kwargs = dict(kwargs)
        if deadline is not None:
            remaining = deadline - clock()
            if remaining <= 0:
                raise RemoteServiceError(WAIT_TIMEOUT_MESSAGE)
            request_kwargs["timeout"] = min(request_timeout, max(0.001, remaining))
        try:
            return _request_json(url, api_key, **request_kwargs)
        except RemoteServiceConnectionError as exc:
            if deadline is not None:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise RemoteServiceError(WAIT_TIMEOUT_MESSAGE) from None
            if attempt >= len(NETWORK_RETRY_DELAYS):
                raise
            delay = exc.retry_after if exc.retry_after is not None else NETWORK_RETRY_DELAYS[attempt]
            if deadline is not None and delay >= remaining:
                raise RemoteServiceError(WAIT_TIMEOUT_MESSAGE) from None
            sleep(delay)
    raise AssertionError("unreachable")


def _poll_delay(payload):
    try:
        milliseconds = int(payload.get("poll_after_ms") or 1000)
    except (TypeError, ValueError):
        milliseconds = 1000
    return max(0.25, min(milliseconds / 1000, 5.0))


def _raise_terminal_error(payload):
    error = payload.get("error")
    if isinstance(error, dict):
        code = _service_text(error.get("code") or "RECORD_UNAVAILABLE", 80)
        message = _service_text(error.get("message") or "牌谱获取失败")
        raise RemoteServiceError(f"远程牌谱服务返回 {code}：{message}")
    raise RemoteServiceError("远程牌谱服务未能获取这条牌谱。")


def _validate_result(result):
    if not isinstance(result.get("name"), list) or not isinstance(result.get("log"), list):
        raise RemoteServiceError("远程牌谱服务返回的 Tenhou JSON 缺少 name 或 log。")
    if "ver" not in result:
        raise RemoteServiceError("远程牌谱服务返回的 Tenhou JSON 缺少 ver。")
    target_actor = result.get("_target_actor")
    if target_actor is not None and (not isinstance(target_actor, int) or target_actor not in (0, 1, 2, 3)):
        raise RemoteServiceError("远程牌谱服务返回的目标玩家位置无效。")


def fetch_remote_tenhou(
    share_url,
    out_path,
    *,
    status_callback: Callable[[str], None] | None = None,
    max_wait_seconds=900,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
):
    config = load_remote_service_config()
    try:
        wait_seconds = float(max_wait_seconds)
    except (TypeError, ValueError) as exc:
        raise RemoteServiceError("远程牌谱服务等待时间无效。") from exc
    if not math.isfinite(wait_seconds) or wait_seconds <= 0:
        raise RemoteServiceError("远程牌谱服务等待时间无效。")
    deadline = clock() + max(1, wait_seconds)
    idempotency_key = str(uuid4())
    create_url = f"{config.service_url}/api/v1/client/requests"
    payload = _request_json_with_retries(
        create_url,
        config.api_key,
        method="POST",
        body={"share_url": share_url},
        extra_headers={"Idempotency-Key": idempotency_key},
        sleep=sleep,
        clock=clock,
        deadline=deadline,
    )
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise RemoteServiceError("远程牌谱服务没有返回 request_id。")
    if len(request_id) > 256:
        raise RemoteServiceError("远程牌谱服务返回的 request_id 过长。")

    while True:
        state = str(payload.get("status") or "").strip()
        if status_callback:
            status_callback(state)
        if state == "ready":
            break
        if state in ("failed", "expired"):
            _raise_terminal_error(payload)
        if state not in PENDING_STATES:
            raise RemoteServiceError(f"远程牌谱服务返回了未知状态：{_service_text(state, 80) or 'empty'}")
        remaining = deadline - clock()
        if remaining <= 0:
            raise RemoteServiceError(WAIT_TIMEOUT_MESSAGE)
        sleep(min(_poll_delay(payload), remaining))
        if clock() >= deadline:
            raise RemoteServiceError(WAIT_TIMEOUT_MESSAGE)
        status_url = f"{config.service_url}/api/v1/requests/{urllib.parse.quote(request_id, safe='')}"
        payload = _request_json_with_retries(
            status_url,
            config.api_key,
            sleep=sleep,
            clock=clock,
            deadline=deadline,
        )

    result_url = f"{config.service_url}/api/v1/requests/{urllib.parse.quote(request_id, safe='')}/result"
    result = _request_json_with_retries(
        result_url,
        config.api_key,
        timeout=30,
        max_bytes=MAX_RESULT_BYTES,
        sleep=sleep,
        clock=clock,
        deadline=deadline,
    )
    _validate_result(result)

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, allow_nan=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path
