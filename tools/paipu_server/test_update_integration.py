"""Opt-in real process handoff test; only temporary app copies and ephemeral keys.

Run: python -B tools/paipu_server/test_update_integration.py --run
The fixture replaces only network transport. Signature verification, extraction,
dependency validation, journaling, process exit/restart and HTTP APIs are real.
"""
import base64
import hashlib
import http.client
import importlib.util
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def request(port, method, path, body=None, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Mortal-Update-Token"] = token
    try:
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def fixture_server(root, assets, port):
    sys.path.insert(0, str(Path(root) / "tools/paipu_server"))
    import app_updater
    import server

    def fixture_download(url, *, limit, destination=None, expected_hash=None, expected_size=None, progress=None):
        if url == app_updater.RELEASE_API:
            data = (Path(assets) / "release.json").read_bytes()
        else:
            name = url.rsplit("/", 1)[-1]
            if name not in {"mortal-update.json", "mortal-update.sig", "mortal-update-2.2.1.zip"}:
                raise AssertionError("Unexpected test download")
            data = (Path(assets) / name).read_bytes()
        assert len(data) <= limit
        if expected_size is not None:
            assert expected_size == len(data)
        if expected_hash:
            assert expected_hash == hashlib.sha256(data).hexdigest()
        if destination:
            Path(destination).write_bytes(data)
            if progress:
                progress(len(data))
            return None
        return data

    app_updater.download = fixture_download
    sys.argv = ["server.py", "--host", "127.0.0.1", "--port", str(port)]
    server.main()


def run():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    spec = importlib.util.spec_from_file_location("publisher_test", ROOT / "scripts/build-update.py")
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    import app_updater
    with tempfile.TemporaryDirectory(prefix="mortal-real-update-") as temp:
        folder = Path(temp).resolve()
        root, assets = folder / "便携 测试", folder / "signed-assets"
        for name, data in publisher.source_files(ROOT).items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        key = Ed25519PrivateKey.generate()
        public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
        app_updater.atomic_json(root / "update-trust.json", {"schema": 1, "public_key": public})
        app_updater.atomic_json(root / "app-version.json", {"version": "2.2.0", "runtime_abi": "win-amd64-cp312", "updater_protocol": 1})
        sentinels = {"paipu-service.local.json": b"private-user-settings", "viewer-data/history.txt": b"history",
                     "mj_model/user-model.pth": b"do-not-replace-model", "tmp/paipu_jobs/personal.txt": b"personal-record"}
        for name, data in sentinels.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        manifest = publisher.build(root, assets, "2.2.1", key, "Integration fixture, never published")
        prefix = f"https://github.com/{app_updater.REPOSITORY}/releases/download/v2.2.1/"
        app_updater.atomic_json(assets / "release.json", {"tag_name": "v2.2.1", "assets": [
            {"name": name, "browser_download_url": prefix + name} for name in ("mortal-update.json", "mortal-update.sig")
        ]})
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = os.environ.copy()
        initial_probe = "fixture-" + secrets.token_hex(16)
        env["MORTAL_UPDATE_HEALTH_TOKEN"] = initial_probe  # suppress real network checks
        process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--fixture-server",
                                    str(root), str(assets), str(port)], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **app_updater.hidden_process_options())
        expected_probes = {initial_probe}
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    status, result = request(port, "GET", "/api/update/status")
                    if status == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError("Fixture server did not start")
                time.sleep(0.2)
            token = result["csrf_token"]
            status, _ = request(port, "POST", "/api/update/check", {}, token)
            assert status == 200
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, result = request(port, "GET", "/api/update/status")
                if result["state"] == "available":
                    break
                time.sleep(0.1)
            assert result["state"] == "available", result
            status, result = request(port, "POST", "/api/update/install", {"version": "2.2.1"}, token)
            assert status == 200, result
            deadline = time.monotonic() + 100
            last_state = None
            while time.monotonic() < deadline:
                for plan in (root / ".updates").glob("*/plan.json"):
                    expected_probes.add(json.loads(plan.read_text())["health_token"])
                try:
                    _, result = request(port, "GET", "/api/update/status")
                    if result.get("state") != last_state:
                        print("integration:", result.get("state"), result.get("current_version"), flush=True)
                        last_state = result.get("state")
                    if result.get("state") == "done" and result.get("current_version") == "2.2.1":
                        break
                    if result.get("state") in ("error", "rolled_back", "recovery_required"):
                        raise AssertionError(result)
                except (OSError, http.client.HTTPException):
                    pass  # expected gap while the old server exits
                time.sleep(0.25)
            else:
                raise AssertionError("Update handoff did not finish")
            assert process.wait(timeout=10) == 0
            for name, data in sentinels.items():
                assert (root / name).read_bytes() == data, name
            assert not (root / ".updates/pending.json").exists()
            assert app_updater.app_info(root)["version"] == "2.2.1"
            print(f"PASS: real process handoff 2.2.0 -> 2.2.1; {len(manifest['files'])} verified files; user data preserved.", flush=True)
        finally:
            # Kill only the test process authenticated by our random probe, never a port-only PID.
            try:
                _, health = request(port, "GET", "/api/update/health")
                if health.get("token") in expected_probes and type(health.get("pid")) is int and health["pid"] > 0:
                    os.kill(health["pid"], signal.SIGTERM)
            except (OSError, http.client.HTTPException):
                pass
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            # Allow Windows to close inherited library/file handles before temp cleanup.
            time.sleep(0.5)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fixture-server":
        fixture_server(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif sys.argv[1:] == ["--run"]:
        run()
    else:
        raise SystemExit("Use --run to run the isolated integration test")
