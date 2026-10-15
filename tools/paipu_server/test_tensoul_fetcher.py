import asyncio
import hashlib
import hmac
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("local_tensoul_test", ROOT / "mortal/tensoul_fetcher.py")
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)
pb = fetcher.pb


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class LocalTensoulTest(unittest.IsolatedAsyncioTestCase):
    def test_handshake_platform_survives_old_protocol_serialization(self):
        # Decode with an independent current schema: testing the actual wire field
        # catches a silent omission even though the old Python class has no attribute.
        file = descriptor_pb2.FileDescriptorProto(name="route_test.proto", syntax="proto3")
        message = file.message_type.add(name="CurrentConnection")
        for name, number, field_type in (("type", 2, 13), ("route_id", 3, 9),
                                         ("timestamp", 4, 4), ("platform", 6, 9)):
            message.field.add(name=name, number=number, type=field_type, label=1)
        pool = descriptor_pool.DescriptorPool()
        pool.Add(file)
        current = message_factory.GetMessageClass(pool.FindMessageTypeByName("CurrentConnection"))
        with patch.object(fetcher.time, "time", return_value=1800000000.123):
            request = fetcher.connection_request("route-2")
        decoded = current.FromString(request.SerializeToString())
        self.assertEqual(decoded.platform, "Web")
        self.assertEqual(decoded.timestamp, 1800000000)
        self.assertEqual((decoded.type, decoded.route_id), (1, "route-2"))
        # Newer ms-api versions should also work, without adding duplicate fields.
        with patch.object(fetcher.pb, "ReqRequestConnection", current):
            self.assertEqual(fetcher.connection_request("route-3").platform, "Web")

    def test_reads_unity_package_version_instead_of_legacy_web_version(self):
        html = 'legacy="0.11.252.w"; createUnityInstance(canvas,{productVersion:"4.0.47"});'
        self.assertEqual(fetcher.current_package_version(html), "4.0.47")
        for bad in ('version="0.11.252.w"', 'productVersion:"4.0.47",productVersion:"4.0.48"'):
            with self.subTest(html=bad), self.assertRaises(fetcher.TensoulFetchError):
                fetcher.current_package_version(bad)

    def test_only_allows_official_tls_urls(self):
        self.assertEqual(fetcher.checked_url("https://record-old.maj-soul.com:9443/record"),
                         "https://record-old.maj-soul.com:9443/record")
        for url in ("http://game.maj-soul.com/a", "https://127.0.0.1/a",
                    "https://game.maj-soul.com.evil.example/a", "https://user@game.maj-soul.com/a",
                    "file:///secret", "https://game.maj-soul.com:8000/a"):
            with self.subTest(url=url), self.assertRaises(fetcher.TensoulFetchError):
                fetcher.checked_url(url)

    def test_gateway_selection_keeps_route_ids_and_prefers_higher_level(self):
        rows = [
            {"id": "route-3", "domain": "route-3.maj-soul.com:8443", "ssl": True,
             "state": "idle", "level": 0, "order": 1},
            {"id": "route-2", "domain": "route-2.maj-soul.com:443", "ssl": True,
             "state": "idle", "level": 1, "order": 2},
            {"id": "private", "domain": "127.0.0.1", "ssl": True,
             "state": "idle", "level": 999, "order": 0},
        ]
        choices = fetcher.gateway_candidates({"data": {"routes": rows}})
        self.assertEqual(choices, [("wss://route-2.maj-soul.com:443/gateway", "route-2"),
                                  ("wss://route-3.maj-soul.com:8443/gateway", "route-3")])

    def lobby(self, login_code=0, fetch_code=0):
        return type("Lobby", (), {
            "login": AsyncMock(return_value=pb.ResLogin(
                account_id=123 if not login_code else 0, error=pb.Error(code=login_code),
            )),
            "login_beat": AsyncMock(return_value=pb.ResCommon()),
            "login_success": AsyncMock(return_value=pb.ResCommon()),
            "fetch_game_record": AsyncMock(return_value=pb.ResGameRecord(
                data=b"dummy-record", error=pb.Error(code=fetch_code),
            )),
        })()

    def client(self, lobby):
        client = fetcher.LocalFetcher()
        client.discover = AsyncMock(return_value=("4.0.47", "0.16.283", []))
        client.connect = AsyncMock(return_value=lobby)
        return client

    async def test_current_version_and_hashed_password_are_used_for_login_and_fetch(self):
        lobby = self.lobby()
        client = self.client(lobby)
        progress = []
        client.callback = lambda stage, value: progress.append((stage, value))
        with (patch.object(fetcher.aiohttp, "ClientSession", return_value=FakeSession()),
              patch.object(fetcher.MajsoulPaipuDownloader, "_handle_game_record",
                           return_value={"log": []}) as convert):
            result = await client.fetch("dummy-id", "test-account", "test-secret")
        request = lobby.login.await_args.args[0]
        self.assertEqual(request.client_version_string, "WebGL_2022-0.16.283")
        self.assertEqual(request.client_version.package, "4.0.47")
        self.assertEqual(request.client_version.resource, "0.16.283")
        self.assertNotIn("0.11.252", str(request.client_version))
        self.assertEqual(request.password, hmac.new(
            b"lailai", b"test-secret", hashlib.sha256,
        ).hexdigest())
        self.assertTrue(request.device.is_browser)
        self.assertEqual(lobby.fetch_game_record.await_args.args[0].client_version_string,
                         "WebGL_2022-0.16.283")
        self.assertEqual(result, {"log": []})
        self.assertEqual([p[1] for p in progress], [30, 38, 42])
        convert.assert_called_once()
        lobby.login_beat.assert_awaited_once()
        lobby.login_success.assert_awaited_once()

    def manifest_environment(self, asset=None, bundle_name="version_bundle.majset"):
        row = asset or {"assetPath": "MyAssets/docs_version/version.json", "ownerBundleIndex": 0}
        tree = {"assetInfos": [row], "bundleInfos": [{"name": bundle_name}]}
        obj = SimpleNamespace(type=SimpleNamespace(name="MonoBehaviour"), read_typetree=lambda: tree)
        return SimpleNamespace(objects=[obj])

    def test_version_bundle_is_resolved_from_manifest_instead_of_hardcoded_filename(self):
        env = self.manifest_environment(bundle_name="version_changed_hash.majset")
        with patch.object(fetcher, "read_unity_bundle", return_value=env):
            self.assertEqual(fetcher.resource_version_bundle_name(b"manifest"),
                             "version_changed_hash.majset")

    def test_version_manifest_rejects_unsafe_or_missing_resource(self):
        cases = [
            self.manifest_environment(bundle_name="../../private.majset"),
            self.manifest_environment(asset={"assetPath": "MyAssets/docs_version/version.json",
                                             "ownerBundleIndex": -1}),
            self.manifest_environment(asset={"assetPath": "unrelated.json", "ownerBundleIndex": 0}),
        ]
        for env in cases:
            with self.subTest(env=env), patch.object(fetcher, "read_unity_bundle", return_value=env):
                with self.assertRaises(fetcher.TensoulFetchError):
                    fetcher.resource_version_bundle_name(b"manifest")

    def test_resource_version_is_read_from_official_document_not_package(self):
        pointer = SimpleNamespace(read=lambda: SimpleNamespace(
            m_Script='{"version":"0.16.283","platform":"WebGL"}',
        ))
        env = SimpleNamespace(container={"myassets/docs_version/version.json": pointer})
        with patch.object(fetcher, "read_unity_bundle", return_value=env):
            self.assertEqual(fetcher.current_resource_version(b"version-bundle"), "0.16.283")

    def test_invalid_resource_version_does_not_fall_back_to_package_or_legacy(self):
        for payload in ('{"version":"0.11.252.w","platform":"WebGL"}',
                        '{"version":"0.16.283","platform":"Android"}', '{}', 'not-json'):
            pointer = SimpleNamespace(read=lambda: SimpleNamespace(m_Script=payload))
            env = SimpleNamespace(container={"myassets/docs_version/version.json": pointer})
            with self.subTest(payload=payload), patch.object(fetcher, "read_unity_bundle", return_value=env):
                with self.assertRaises(fetcher.TensoulFetchError):
                    fetcher.current_resource_version(b"bundle")

    async def test_discovery_fetches_package_and_resource_versions_independently(self):
        client = fetcher.LocalFetcher()
        client.fetch_bytes = AsyncMock(side_effect=[
            b'productVersion: "4.0.47"', b"manifest", b"resource-version",
        ])
        client.fetch_json = AsyncMock(side_effect=[
            {"version": "0.11.252.w"},
            {"ip": [{"gateways": [{"url": "https://route-2.maj-soul.com"}]}]},
            {"data": {"routes": [{"id": "route-2", "domain": "route-2.maj-soul.com",
                                    "ssl": True, "state": "idle", "level": 1, "order": 2}]}},
        ])
        with (patch.object(fetcher, "resource_version_bundle_name", return_value="latest.majset"),
              patch.object(fetcher, "current_resource_version", return_value="0.16.283")):
            package, resource, choices = await client.discover(object())
        self.assertEqual((package, resource), ("4.0.47", "0.16.283"))
        self.assertEqual(len(choices), 1)
        self.assertEqual(client.fetch_bytes.await_args_list[2].args[1],
                         fetcher.ASSET_BASE + "latest.majset")
        self.assertEqual(client.fetch_json.await_args.kwargs["params"],
                         {"platform": "Web", "version": "4.0.47", "lang": "chs_t"})

    async def test_missing_resource_version_stops_before_credentials_are_sent(self):
        lobby = self.lobby()
        client = self.client(lobby)
        client.discover = AsyncMock(side_effect=fetcher.TensoulFetchError("版本检测", "无法读取资源版本"))
        with patch.object(fetcher.aiohttp, "ClientSession", return_value=FakeSession()):
            with self.assertRaises(fetcher.TensoulFetchError):
                await client.fetch("dummy", "account", "secret")
        client.connect.assert_not_awaited()
        lobby.login.assert_not_awaited()

    async def test_login_rejection_is_terminal_and_does_not_fetch_or_retry_credentials(self):
        lobby = self.lobby(login_code=151)
        client = self.client(lobby)
        client.channel = type("Channel", (), {"close": AsyncMock()})()
        output = io.StringIO()
        with (patch.object(fetcher.aiohttp, "ClientSession", return_value=FakeSession()),
              redirect_stderr(output), self.assertRaises(fetcher.TensoulFetchError) as caught):
            await client.fetch("private-record", "private-account", "private-password")
        self.assertEqual(caught.exception.code, 151)
        self.assertIn("151", str(caught.exception))
        self.assertNotIn("private", str(caught.exception) + output.getvalue())
        lobby.login.assert_awaited_once()
        lobby.fetch_game_record.assert_not_awaited()
        client.channel.close.assert_awaited_once()

    async def test_540_is_reported_once_without_automatic_fetch_retry(self):
        lobby = self.lobby(fetch_code=540)
        with (patch.object(fetcher.aiohttp, "ClientSession", return_value=FakeSession()),
              self.assertRaises(fetcher.TensoulFetchError) as caught):
            await self.client(lobby).fetch("dummy-id", "account", "secret")
        self.assertEqual(caught.exception.code, 540)
        lobby.fetch_game_record.assert_awaited_once()

    async def test_library_base_exception_is_normalized_without_protobuf_body(self):
        private_response = pb.ResLogin(error=pb.Error(code=151, str_params=["private-password"]))
        for error in (fetcher.MajsoulLoginError(private_response), fetcher.MajsoulDownloadError(540)):
            client = fetcher.LocalFetcher()
            client.workflow = AsyncMock(side_effect=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(fetcher.TensoulFetchError) as caught:
                await client.fetch("dummy-id", "account", "secret")
            self.assertNotIn("private-password", str(caught.exception))
            self.assertIn(str(caught.exception.code), str(caught.exception))

    async def test_timeout_is_terminal_with_current_stage(self):
        client = fetcher.LocalFetcher()
        client.stage = "登录雀魂"
        client.workflow = AsyncMock(side_effect=asyncio.TimeoutError())
        with self.assertRaisesRegex(fetcher.TensoulFetchError, "登录雀魂超时"):
            await client.fetch("dummy-id", "account", "secret")

    async def test_total_deadline_cancels_a_stalled_workflow(self):
        client = fetcher.LocalFetcher()
        client.stage = "连接雀魂"

        async def blocked(*args):
            await asyncio.Event().wait()

        client.workflow = blocked
        with patch.object(fetcher, "TOTAL_TIMEOUT", 0.02):
            with self.assertRaisesRegex(fetcher.TensoulFetchError, "连接雀魂超时"):
                await client.fetch("dummy-id", "account", "secret")

    async def test_close_has_a_deadline(self):
        async def blocked():
            await asyncio.Event().wait()

        channel = type("Channel", (), {"close": staticmethod(blocked)})()
        with patch.object(fetcher, "CLOSE_TIMEOUT", 0.02):
            await asyncio.wait_for(fetcher.close_channel(channel), timeout=0.3)

    async def test_http_size_limit_and_no_redirects(self):
        class Content:
            async def iter_chunked(self, size):
                yield b"12345"
                yield b"67890"

        response = FakeSession()
        response.status = 200
        response.content = Content()
        session = type("Session", (), {"get": Mock(return_value=response)})()
        client = fetcher.LocalFetcher()
        with self.assertRaisesRegex(fetcher.TensoulFetchError, "过大"):
            await client.fetch_bytes(session, fetcher.MS_HOST + "/1/", "读取版本", limit=6)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        response.status = 302
        with self.assertRaisesRegex(fetcher.TensoulFetchError, "HTTP 302"):
            await client.fetch_bytes(session, fetcher.MS_HOST + "/1/", "读取版本")

    async def test_record_download_url_uses_bounded_reader(self):
        lobby = self.lobby()
        lobby.fetch_game_record.return_value = pb.ResGameRecord(
            data_url="https://record-old.maj-soul.com:9443/record",
        )
        client = self.client(lobby)
        client.fetch_bytes = AsyncMock(return_value=b"downloaded-record")
        with (patch.object(fetcher.aiohttp, "ClientSession", return_value=FakeSession()),
              patch.object(fetcher.MajsoulPaipuDownloader, "_handle_game_record",
                           return_value={"log": []}) as convert):
            await client.fetch("dummy-id", "account", "secret")
        self.assertEqual(client.fetch_bytes.await_args.kwargs["limit"], fetcher.MAX_RECORD_BYTES)
        self.assertEqual(convert.call_args.args[0].data, b"downloaded-record")

    async def test_cancellation_propagates_but_transport_is_closed(self):
        client = fetcher.LocalFetcher()
        client.workflow = AsyncMock(side_effect=asyncio.CancelledError())
        client.channel = type("Channel", (), {"close": AsyncMock()})()
        with self.assertRaises(asyncio.CancelledError):
            await client.fetch("dummy-id", "account", "secret")
        client.channel.close.assert_awaited_once()

    async def test_close_failure_does_not_hide_login_error(self):
        client = fetcher.LocalFetcher()
        client.workflow = AsyncMock(side_effect=fetcher.upstream_error("登录雀魂", 151))
        client.channel = type("Channel", (), {"close": AsyncMock(side_effect=RuntimeError("close"))})()
        with self.assertRaises(fetcher.TensoulFetchError) as caught:
            await client.fetch("dummy-id", "account", "secret")
        self.assertEqual(caught.exception.code, 151)

    async def test_connection_handshake_uses_discovered_route_id(self):
        channel = type("Channel", (), {"connect": AsyncMock(), "close": AsyncMock()})()
        route = type("Route", (), {
            "request_connection": AsyncMock(return_value=pb.ResRequestConnection(result=1)),
            "heartbeat": AsyncMock(return_value=pb.ResCommon()),
        })()
        client = fetcher.LocalFetcher()
        with (patch.object(fetcher, "LocalChannel", return_value=channel),
              patch.object(fetcher, "Route", return_value=route),
              patch.object(fetcher, "Lobby", return_value=object())):
            await client.connect([("wss://route-2.maj-soul.com/gateway", "route-2")])
        req = route.request_connection.await_args.args[0]
        self.assertEqual((req.route_id, req.type), ("route-2", 1))
        self.assertGreater(req.timestamp, 0)
        client.heartbeat.cancel()
        await asyncio.gather(client.heartbeat, return_exceptions=True)
        await fetcher.close_channel(channel)

    async def test_nonusable_handshake_does_not_send_login(self):
        channel = type("Channel", (), {"connect": AsyncMock(), "close": AsyncMock()})()
        route = type("Route", (), {
            "request_connection": AsyncMock(return_value=pb.ResRequestConnection(result=0)),
            "heartbeat": AsyncMock(),
        })()
        client = fetcher.LocalFetcher()
        with (patch.object(fetcher, "LocalChannel", return_value=channel),
              patch.object(fetcher, "Route", return_value=route),
              patch.object(fetcher, "Lobby") as lobby):
            with self.assertRaises(fetcher.TensoulFetchError):
                await client.connect([("wss://route-2.maj-soul.com/gateway", "route-2")])
        lobby.assert_not_called()
        route.heartbeat.assert_not_awaited()
        channel.close.assert_awaited_once()

    async def test_cancelled_connection_attempt_is_cleaned_up(self):
        channel = type("Channel", (), {
            "connect": AsyncMock(side_effect=asyncio.CancelledError()), "close": AsyncMock(),
        })()
        with patch.object(fetcher, "LocalChannel", return_value=channel):
            with self.assertRaises(asyncio.CancelledError):
                await fetcher.LocalFetcher().connect([
                    ("wss://route-2.maj-soul.com/gateway", "route-2"),
                ])
        channel.close.assert_awaited_once()

    async def test_failed_download_does_not_replace_existing_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.json"
            output.write_text("previous", encoding="utf-8")
            with patch.object(fetcher.LocalFetcher, "fetch", new=AsyncMock(
                side_effect=fetcher.upstream_error("登录雀魂", 151),
            )):
                with self.assertRaises(fetcher.TensoulFetchError):
                    await fetcher.download("dummy", output, "account", "secret")
            self.assertEqual(output.read_text(encoding="utf-8"), "previous")
            self.assertEqual(list(Path(tmp).glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
