"""Local CN transport using current client metadata and tensoul's converter.

Credentials stay in memory. Never log login responses, URLs with record identifiers,
or exception bodies returned by the upstream client.
"""

import asyncio
import hashlib
import hmac
import json
import re
import sys
import time
import warnings
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
import websockets
from ms import protocol_pb2 as pb
from ms.base import MSRPCChannel
from ms.rpc import Lobby, Route
from tensoul import MajsoulPaipuDownloader
from tensoul.downloader import MajsoulDownloadError, MajsoulLoginError


MS_HOST = "https://game.maj-soul.com"
ASSET_BASE = MS_HOST + "/assetbundles/ASTC/"
TOTAL_TIMEOUT = 180
RPC_TIMEOUT = 30
CLOSE_TIMEOUT = 5
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_VERSION_BUNDLE_BYTES = 4 * 1024 * 1024
LOGIN_BEAT_CONTRACT = "DF2vkXCnfeXp4WoGrBGNcJBufZiMN3uP"


class TensoulFetchError(RuntimeError):
    def __init__(self, stage, message, code=None):
        self.stage = stage
        self.code = code
        super().__init__(message)


def upstream_error(stage, code):
    messages = {
        151: "雀魂拒绝了客户端版本，请更新本地获取组件；这不代表密码错误。",
        540: "雀魂牌谱拉取次数受限，请稍后手动重试。",
        541: "雀魂拒绝返回这份牌谱，尚无已核实的具体原因。",
        1004: "雀魂登录连接已失效，请重新获取。",
        1203: "雀魂提示这局游戏不存在。",
        1204: "雀魂提示不满足访问条件。",
    }
    message = messages.get(code, "雀魂拒绝了本次请求，请核对账号类型、登录状态或访问权限。")
    return TensoulFetchError(stage, f"{stage}失败（雀魂错误码 {code}）：{message}", code)


def check_response(response, stage):
    if response.HasField("error") and response.error.code:
        raise upstream_error(stage, int(response.error.code))


def library_error(error, stage):
    code = getattr(error, "code", None)
    if code is None and error.args:
        code = getattr(getattr(error.args[0], "error", None), "code", None)
    if type(code) is int and code > 0:
        return upstream_error(stage, code)
    return TensoulFetchError(stage, f"{stage}失败（{type(error).__name__}），请重新获取。")


def connection_request(route_id):
    request = pb.ReqRequestConnection(
        type=1, route_id=route_id, timestamp=int(time.time()),
    )
    # Current official cli_route_pb / NetRoute adds string field 6: platform="Web".
    # ms-api 0.11.100 lacks it. Protobuf preserves unknown fields on serialization,
    # so keep this compatibility fix local instead of modifying installed packages.
    if "platform" in request.DESCRIPTOR.fields_by_name:
        request.platform = "Web"
    else:
        request.MergeFromString(b"\x32\x03Web")
    return request


def current_package_version(html):
    versions = set(re.findall(
        r'''\bproductVersion\s*:\s*["'](\d{1,3}(?:\.\d{1,5}){2,3})["']''', html,
    ))
    if len(versions) != 1:
        raise TensoulFetchError("版本检测", "无法识别雀魂当前客户端版本，请更新本地获取组件。")
    return versions.pop()


def read_unity_bundle(data):
    """Read public metadata only; no downloaded scripts are executed or extracted."""
    try:
        import UnityPy
    except ImportError:
        raise TensoulFetchError(
            "版本检测", "缺少 UnityPy 版本解析组件，请运行启动脚本或“检查环境.cmd”补齐依赖。",
        ) from None
    if len(data) > MAX_VERSION_BUNDLE_BYTES or not data.startswith(b"UnityFS\0"):
        raise TensoulFetchError("版本检测", "雀魂版本资源格式不正确或体积超出限制。")
    try:
        UnityPy.config.FALLBACK_UNITY_VERSION = "2022.3.0f1"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            return UnityPy.load(data)
    except Exception:
        raise TensoulFetchError("版本检测", "无法解析雀魂官方版本资源，请更新本地获取组件。") from None


def resource_version_bundle_name(data):
    environment = read_unity_bundle(data)
    matches = set()
    try:
        for obj in environment.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            table = obj.read_typetree()
            bundles = table.get("bundleInfos", [])
            for asset in table.get("assetInfos", []):
                if asset.get("assetPath", "").replace("\\", "/").lower() != "myassets/docs_version/version.json":
                    continue
                index = asset.get("ownerBundleIndex")
                if type(index) is not int or not 0 <= index < len(bundles):
                    raise ValueError("invalid bundle index")
                name = bundles[index].get("name", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}\.majset", name):
                    raise ValueError("invalid bundle name")
                matches.add(name)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise TensoulFetchError("版本检测", "雀魂官方资源清单格式已变化，已停止登录。") from None
    if len(matches) != 1:
        raise TensoulFetchError("版本检测", "无法唯一定位雀魂当前资源版本，已停止登录。")
    return matches.pop()


def current_resource_version(data):
    environment = read_unity_bundle(data)
    versions = set()
    try:
        for path, pointer in environment.container.items():
            normalized = path.replace("\\", "/").lower()
            if normalized != "myassets/docs_version/version.json":
                continue
            text = pointer.read().m_Script
            if not isinstance(text, str) or len(text) > 16384:
                raise ValueError("invalid version document")
            document = json.loads(text)
            version = document.get("version")
            if document.get("platform") != "WebGL" or not isinstance(version, str):
                raise ValueError("invalid version platform")
            if not re.fullmatch(r"\d{1,3}(?:\.\d{1,5}){2,3}", version):
                raise ValueError("invalid resource version")
            versions.add(version)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise TensoulFetchError("版本检测", "雀魂资源版本数据无效，已停止登录。") from None
    if len(versions) != 1:
        raise TensoulFetchError("版本检测", "无法读取雀魂当前资源版本，已停止登录。")
    return versions.pop()


def checked_url(url, *, websocket=False):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        valid = (
            parsed.scheme == ("wss" if websocket else "https")
            and host.endswith(".maj-soul.com")
            and parsed.port in (None, 443, 8443, 9443)
            and parsed.username is None and parsed.password is None
            and not parsed.fragment and "\\" not in url
        )
        if websocket:
            valid = valid and parsed.path == "/gateway" and not parsed.query
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise TensoulFetchError("线路检测", "雀魂返回了未允许的线路地址，已停止请求。")
    return url


def gateway_candidates(payload):
    rows = payload.get("data", {}).get("routes", [])
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or row.get("ssl") is not True:
            continue
        domain, route_id = row.get("domain"), row.get("id")
        if not isinstance(domain, str) or not isinstance(route_id, str):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", route_id):
            continue
        try:
            endpoint = checked_url(f"wss://{domain}/gateway", websocket=True)
        except TensoulFetchError:
            continue
        level = row.get("level", 0)
        order = row.get("order", 999)
        if type(level) is not int or type(order) is not int:
            continue
        candidates.append((row.get("state") != "idle", -level, order, endpoint, route_id))
    result = []
    seen = set()
    for _, _, _, endpoint, route_id in sorted(candidates):
        if endpoint not in seen:
            result.append((endpoint, route_id))
            seen.add(endpoint)
    if not result:
        raise TensoulFetchError("线路检测", "没有检测到可用的雀魂安全网关线路。")
    return result[:5]


class LocalChannel(MSRPCChannel):
    async def connect(self, origin):
        self._ws = await websockets.connect(
            self._endpoint, origin=origin, open_timeout=10, close_timeout=3,
            max_size=MAX_RECORD_BYTES,
        )
        self._msg_dispatcher = asyncio.create_task(self.dispatch_msg())

    async def close(self):
        task = self._msg_dispatcher
        self._msg_dispatcher = None
        try:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        finally:
            if self._ws is not None:
                await self._ws.close()
                self._ws = None
            self._req_events.clear()
            self._res.clear()


async def close_channel(channel):
    if channel is not None:
        with suppress(Exception):
            await asyncio.wait_for(channel.close(), timeout=CLOSE_TIMEOUT)


class LocalFetcher:
    def __init__(self, callback=None):
        self.callback = callback
        self.stage = "初始化"
        self.channel = None
        self.heartbeat = None

    def progress(self, stage, value):
        self.stage = stage
        print(f"tensoul: {stage}", file=sys.stderr, flush=True)
        if self.callback is not None:
            self.callback(stage, value)

    async def fetch_bytes(self, session, url, label, *, params=None, limit=MAX_DOCUMENT_BYTES):
        checked_url(url)
        for attempt in range(2):
            try:
                async with session.get(url, params=params, allow_redirects=False) as response:
                    if response.status != 200:
                        raise TensoulFetchError(label, f"{label}失败（HTTP {response.status}）。")
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise TensoulFetchError(label, f"{label}返回的数据过大，已停止读取。")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except (asyncio.TimeoutError, OSError, aiohttp.ClientError) as error:
                if attempt == 0:
                    await asyncio.sleep(1)
                else:
                    raise TensoulFetchError(
                        label, f"{label}连接失败（{type(error).__name__}），请检查网络或代理。",
                    ) from None

    async def fetch_json(self, session, url, label, *, params=None):
        data = await self.fetch_bytes(session, url, label, params=params)
        try:
            value = json.loads(data)
            if isinstance(value, dict):
                return value
        except (ValueError, UnicodeError):
            pass
        raise TensoulFetchError(label, f"{label}返回了无法识别的数据。")

    async def discover(self, session):
        self.progress("读取雀魂当前客户端版本", 21)
        page = await self.fetch_bytes(session, MS_HOST + "/1/", "读取官方客户端")
        package_version = current_package_version(page.decode("utf-8"))
        self.progress("读取雀魂当前资源版本", 22)
        manifest = await self.fetch_bytes(
            session, ASSET_BASE + "bundle_info_so.majset", "读取资源版本清单",
            limit=MAX_VERSION_BUNDLE_BYTES,
        )
        bundle_name = await asyncio.to_thread(resource_version_bundle_name, manifest)
        version_bundle = await self.fetch_bytes(
            session, ASSET_BASE + bundle_name, "读取资源版本文件",
            limit=MAX_VERSION_BUNDLE_BYTES,
        )
        resource_version = await asyncio.to_thread(current_resource_version, version_bundle)
        legacy = await self.fetch_json(session, MS_HOST + "/1/version.json", "读取线路配置版本")
        config_version = legacy.get("version", "")
        if not isinstance(config_version, str) or not re.fullmatch(r"[0-9.]+\.w", config_version):
            raise TensoulFetchError("线路检测", "雀魂线路配置版本格式已变化，请更新本地获取组件。")
        config = await self.fetch_json(
            session, MS_HOST + f"/1/v{config_version}/config.json", "读取网关配置",
        )
        self.progress("检测雀魂本地获取线路", 23)
        groups = config.get("ip", [])
        if not groups or not isinstance(groups[0], dict):
            raise TensoulFetchError("线路检测", "雀魂网关配置缺少线路信息。")
        gateways = groups[0].get("gateways") or groups[0].get("region_urls") or []
        last_error = None
        for gateway in gateways[:5]:
            try:
                url = checked_url(gateway["url"].rstrip("/") + "/api/clientgate/routes")
                document = await self.fetch_json(session, url, "读取雀魂线路", params={
                    "platform": "Web", "version": package_version, "lang": "chs_t",
                })
                return package_version, resource_version, gateway_candidates(document)
            except (TensoulFetchError, KeyError, TypeError, ValueError) as error:
                last_error = error
        if isinstance(last_error, TensoulFetchError):
            raise last_error
        raise TensoulFetchError("线路检测", "无法检测雀魂网关线路，请检查网络或代理。")

    async def connect(self, candidates):
        for index, (endpoint, route_id) in enumerate(candidates, 1):
            self.progress(f"连接雀魂网关（{index}/{len(candidates)}）", 25)
            channel = LocalChannel(endpoint)
            try:
                async with asyncio.timeout(20):
                    await channel.connect(MS_HOST)
                    route = Route(channel)
                    response = await route.request_connection(connection_request(route_id))
                    check_response(response, "线路握手")
                    if response.result != 1:
                        raise TensoulFetchError("线路握手", "雀魂网关尚未允许连接，请稍后重试。")
                    check_response(await route.heartbeat(pb.ReqHeartbeat(platform=11)), "线路保活")
            except asyncio.CancelledError:
                await close_channel(channel)
                raise
            except Exception:
                await close_channel(channel)
                continue
            self.channel = channel
            self.heartbeat = asyncio.create_task(self.keepalive(route))
            return Lobby(channel)
        raise TensoulFetchError("连接网关", "无法建立雀魂网关连接，请检查网络或代理后重试。")

    async def keepalive(self, route):
        # Only while this single local download is running; no reconnect or record retry loop.
        try:
            while True:
                await asyncio.sleep(15)
                response = await asyncio.wait_for(
                    route.heartbeat(pb.ReqHeartbeat(platform=11)), timeout=10,
                )
                check_response(response, "线路保活")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Closing the transport makes outstanding bounded RPCs fail safely.
            await close_channel(self.channel)

    async def workflow(self, record_uuid, username, password):
        timeout = aiohttp.ClientTimeout(total=20, connect=10, sock_read=10)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            package_version, resource_version, candidates = await asyncio.wait_for(
                self.discover(session), timeout=90,
            )
            lobby = await self.connect(candidates)
            # Official GameUtility.GetClientVersionString uses GameMgr.Inst.version,
            # i.e. the RESOURCE version, not Unity's productVersion/package version.
            client_version = f"WebGL_2022-{resource_version}"
            self.progress(f"登录雀魂（资源 {resource_version} / 程序 {package_version}）", 30)
            request = pb.ReqLogin(
                account=username,
                password=hmac.new(b"lailai", password.encode("utf-8"), hashlib.sha256).hexdigest(),
                random_key=str(uuid4()), gen_access_token=True, reconnect=False,
                client_version_string=client_version, currency_platforms=[1, 2, 5, 6, 8, 10, 11],
                type=0, tag="cn",
            )
            request.device.CopyFrom(pb.ClientDeviceInfo(
                platform="pc", hardware="pc", os="windows", os_version="win10",
                is_browser=True, software="Chrome", sale_platform="web", screen_type=1,
            ))
            request.client_version.package = package_version
            request.client_version.resource = resource_version
            response = await asyncio.wait_for(lobby.login(request), timeout=RPC_TIMEOUT)
            check_response(response, "登录雀魂")
            if not response.account_id and not response.access_token:
                raise TensoulFetchError("登录雀魂", "雀魂未返回有效登录结果，请检查账号登录方式。")
            check_response(await asyncio.wait_for(lobby.login_beat(
                pb.ReqLoginBeat(contract=LOGIN_BEAT_CONTRACT),
            ), timeout=RPC_TIMEOUT), "确认登录")
            check_response(await asyncio.wait_for(lobby.login_success(pb.ReqCommon()),
                                                 timeout=RPC_TIMEOUT), "确认登录")
            self.progress("获取雀魂牌谱（本地）", 38)
            response = await asyncio.wait_for(lobby.fetch_game_record(pb.ReqGameRecord(
                game_uuid=record_uuid, client_version_string=client_version,
            )), timeout=60)
            check_response(response, "获取牌谱")
            if not response.data and response.data_url:
                response.data = await self.fetch_bytes(
                    session, response.data_url, "下载牌谱数据", limit=MAX_RECORD_BYTES,
                )
            if not response.data:
                raise TensoulFetchError("获取牌谱", "雀魂没有返回牌谱数据。")
            self.progress("使用 tensoul 转换牌谱", 42)
            return MajsoulPaipuDownloader()._handle_game_record(response)

    async def fetch(self, record_uuid, username, password):
        try:
            return await asyncio.wait_for(self.workflow(record_uuid, username, password),
                                          timeout=TOTAL_TIMEOUT)
        except TensoulFetchError:
            raise
        except (MajsoulLoginError, MajsoulDownloadError) as error:
            raise library_error(error, self.stage) from None
        except asyncio.TimeoutError:
            raise TensoulFetchError(self.stage, f"{self.stage}超时，任务已结束，请稍后重试。") from None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise TensoulFetchError(
                self.stage, f"{self.stage}失败（{type(error).__name__}），任务已结束，请重试。",
            ) from None
        finally:
            if self.heartbeat is not None:
                self.heartbeat.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self.heartbeat
            await close_channel(self.channel)


async def download(record_uuid, out_file, username, password, status_callback=None):
    log = await LocalFetcher(status_callback).fetch(record_uuid, username, password)
    out_path = Path(out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(f".{out_path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(out_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return out_path
