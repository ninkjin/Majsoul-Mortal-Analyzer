import argparse
import base64
import hashlib
import json
import os
import random
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def find_browser():
    candidates = [
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    raise FileNotFoundError("找不到 Chrome/Edge。可以设置 CHROME_PATH 指向浏览器 exe。")


def wait_json(url, timeout=10):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"无法连接 Chrome DevTools: {last_error}")


class WebSocket:
    def __init__(self, url):
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        raw = socket.create_connection((self.host, self.port), timeout=8)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) if parsed.scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(req)
        resp = self.sock.recv(4096)
        if b" 101 " not in resp:
            raise RuntimeError(f"WebSocket 握手失败: {resp[:200]!r}")

    def send_json(self, payload):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_json(self, timeout=1):
        self.sock.settimeout(timeout)
        first = self.sock.recv(2)
        if not first:
            raise EOFError
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        data = self._read_exact(length)
        if masked:
            data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        if opcode == 8:
            raise EOFError
        if opcode != 1:
            return None
        return json.loads(data.decode("utf-8"))

    def _read_exact(self, length):
        chunks = []
        remaining = length
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise EOFError
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


def launch_browser(port):
    browser = find_browser()
    profile = Path(tempfile.mkdtemp(prefix="mortal-majsoul-probe-"))
    proc = subprocess.Popen([
        browser,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, profile


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return res.read().decode("utf-8")


def majsoul_resource_overrides():
    version = json.loads(fetch_text("https://game.maj-soul.com/1/version.json"))["version"]
    base = f"https://game.maj-soul.com/1/v{version}"
    return {
        "config.json": fetch_text(f"{base}/config.json"),
        "chs.json": fetch_text(f"{base}/chs.json"),
    }


def send(ws, counter, method, params=None):
    counter[0] += 1
    ws.send_json({"id": counter[0], "method": method, "params": params or {}})
    return counter[0]


def decode_lq_frame(payload):
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        return None
    if len(raw) < 3:
        return None

    offset = 3
    decoded = {
        "kind": raw[0],
        "request_id": int.from_bytes(raw[1:3], "little"),
        "length": len(raw),
    }

    fields = _decode_wrapper_fields(raw[offset:])
    if fields.get("name") is not None:
        decoded["name"] = fields["name"]
    if fields.get("data") is not None:
        data = fields["data"]
        decoded["dataLength"] = len(data)
        decoded["dataSample"] = base64.b64encode(data[:240]).decode("ascii")
    return decoded


def _decode_wrapper_fields(raw):
    fields = {}
    offset = 0
    while offset < len(raw):
        key, offset = _read_varint(raw, offset)
        field_number = key >> 3
        wire_type = key & 7
        if wire_type == 2:
            size, offset = _read_varint(raw, offset)
            value = raw[offset:offset + size]
            offset += size
            if field_number == 1:
                try:
                    fields["name"] = value.decode("utf-8")
                except UnicodeDecodeError:
                    fields["name"] = ""
            elif field_number == 2:
                fields["data"] = value
        elif wire_type == 0:
            _, offset = _read_varint(raw, offset)
        elif wire_type == 5:
            offset += 4
        elif wire_type == 1:
            offset += 8
        else:
            break
    return fields


def _read_varint(raw, offset):
    value = 0
    shift = 0
    while offset < len(raw):
        byte = raw[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("truncated varint")


def probe(url, seconds=35, port=9223, out=None):
    proc, profile = launch_browser(port)
    overrides = majsoul_resource_overrides()
    records = []
    bodies = []
    frames = []
    logs = []
    request_urls = {}
    out = Path(out or ROOT / "tmp" / "majsoul-browser-probe.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        version = wait_json(f"http://127.0.0.1:{port}/json/version")
        ws = WebSocket(version["webSocketDebuggerUrl"])
        counter = [0]
        send(ws, counter, "Target.createTarget", {"url": "about:blank"})
        target_ws = None
        deadline = time.time() + 5
        while time.time() < deadline:
            item = ws.recv_json(timeout=1)
            if item and item.get("id") == counter[0]:
                target_id = item["result"]["targetId"]
                targets = wait_json(f"http://127.0.0.1:{port}/json/list")
                target_ws = next(t["webSocketDebuggerUrl"] for t in targets if t.get("id") == target_id)
                break
        if not target_ws:
            raise RuntimeError("无法创建 Chrome target")

        tab = WebSocket(target_ws)
        counter = [0]
        send(tab, counter, "Network.enable")
        send(tab, counter, "Page.enable")
        send(tab, counter, "Runtime.enable")
        send(tab, counter, "Log.enable")
        send(tab, counter, "Fetch.enable", {
            "patterns": [
                {"urlPattern": "*://game.maj-soul.com/1/config.json*"},
                {"urlPattern": "*://game.maj-soul.com/1/chs.json*"},
            ]
        })
        send(tab, counter, "Page.navigate", {"url": url})

        end = time.time() + seconds
        interesting = re_keywords = ("record", "paipu", "game_record", "replay", "lq.")
        while time.time() < end:
            try:
                msg = tab.recv_json(timeout=1)
            except socket.timeout:
                continue
            if not msg:
                continue
            if "id" in msg and "result" in msg and "body" in msg["result"]:
                body = msg["result"].get("body", "")
                bodies.append({
                    "id": msg["id"],
                    "base64Encoded": msg["result"].get("base64Encoded"),
                    "sample": body[:1200],
                    "length": len(body),
                })
                continue
            if "method" not in msg:
                continue
            method = msg["method"]
            params = msg.get("params", {})
            if method == "Fetch.requestPaused":
                request_url = params.get("request", {}).get("url", "")
                name = "config.json" if "/config.json" in request_url else "chs.json" if "/chs.json" in request_url else None
                if name and name in overrides:
                    body = base64.b64encode(overrides[name].encode("utf-8")).decode("ascii")
                    send(tab, counter, "Fetch.fulfillRequest", {
                        "requestId": params["requestId"],
                        "responseCode": 200,
                        "responseHeaders": [
                            {"name": "Content-Type", "value": "application/json; charset=utf-8"},
                        ],
                        "body": body,
                    })
                else:
                    send(tab, counter, "Fetch.continueRequest", {"requestId": params["requestId"]})
            elif method == "Network.requestWillBeSent":
                req = params.get("request", {})
                request_urls[params.get("requestId")] = req.get("url", "")
            elif method == "Network.responseReceived":
                res = params.get("response", {})
                res_url = res.get("url", "")
                row = {
                    "type": "response",
                    "url": res_url,
                    "status": res.get("status"),
                    "mimeType": res.get("mimeType"),
                }
                records.append(row)
                if any(word in res_url.lower() for word in interesting):
                    rid = params.get("requestId")
                    body_id = send(tab, counter, "Network.getResponseBody", {"requestId": rid})
                    row["bodyRequestId"] = body_id
            elif method == "Network.webSocketCreated":
                records.append({"type": "websocket", "url": params.get("url")})
            elif method in ("Network.webSocketFrameReceived", "Network.webSocketFrameSent"):
                payload = params.get("response", {}).get("payloadData", "")
                decoded = decode_lq_frame(payload)
                if any(word in payload.lower() for word in interesting) or len(frames) < 500:
                    frames.append({
                        "method": method,
                        "length": len(payload),
                        "sample": payload[:500],
                        "decoded": decoded,
                    })
            elif method == "Runtime.consoleAPICalled":
                logs.append({
                    "type": params.get("type"),
                    "args": [
                        arg.get("value", arg.get("description", ""))
                        for arg in params.get("args", [])
                    ],
                })
            elif method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails", {})
                logs.append({
                    "type": "exception",
                    "text": details.get("text"),
                    "url": details.get("url"),
                    "lineNumber": details.get("lineNumber"),
                    "columnNumber": details.get("columnNumber"),
                    "exception": details.get("exception", {}).get("description"),
                })
            elif method == "Log.entryAdded":
                entry = params.get("entry", {})
                logs.append({
                    "type": entry.get("level"),
                    "source": entry.get("source"),
                    "text": entry.get("text"),
                    "url": entry.get("url"),
                    "lineNumber": entry.get("lineNumber"),
                })
        result = {
            "url": url,
            "records": records,
            "bodies": bodies,
            "frames": frames,
            "logs": logs,
        }
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--seconds", type=int, default=35)
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--out")
    args = parser.parse_args()
    out = probe(args.url, seconds=args.seconds, port=args.port, out=args.out)
    print(out)


if __name__ == "__main__":
    main()
