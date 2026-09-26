"""Exercise query-tolerant routes on an unused loopback port."""

import threading
from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from appletv_zhsubs.config import (
    CERT_PATH,
    KEY_PATH,
    LIVE_DIR,
    LOG_PATH,
    REGISTRY_PATH,
    SUB_BASE,
    SUBS_DIR,
    TEST_MASTER,
    ServerConfig,
)
from appletv_zhsubs.hls import build_vtt_playlist
from appletv_zhsubs.registry import Registry
from appletv_zhsubs.server import Handler, create_server
from tests.support import ScratchTestCase


class RouteTests(ScratchTestCase):
    def setUp(self) -> None:
        super().setUp()
        settings = ServerConfig(
            port=0,
            subs_dir=self.scratch,
            registry_path=self.scratch / "registry.json",
            log_path=self.scratch / "server.log",
        )

        class TestHandler(Handler):
            """Use test paths for all reads and writes."""

        TestHandler.settings = settings
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.registry = Registry(settings.registry_path)
        self.vtt = self.scratch / "movie.zh-Hans.vtt"
        self.original = b"WEBVTT\r\n\r\n00:00:01.000 --> 00:00:03.000\r\nhello\r\n"
        self.vtt.write_bytes(self.original)

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        with closing(
            HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        ) as connection:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            headers = dict(response.getheaders())
            self.assertEqual(int(headers["Content-Length"]), len(body))
            self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
            return response.status, headers, body

    def test_health_with_query(self) -> None:
        status, headers, body = self.get("/healthz?probe=1")
        self.assertEqual(
            (status, body, headers["Content-Type"]), (200, b"ok\n", "text/plain")
        )

    def test_test_master_with_query_keeps_exact_bytes(self) -> None:
        status, headers, body = self.get("/t/master.m3u8?cache=1")
        self.assertEqual((status, body), (200, TEST_MASTER.encode()))
        self.assertEqual(headers["Content-Type"], "application/vnd.apple.mpegurl")

    def test_playlist_with_query_keeps_exact_bytes(self) -> None:
        status, headers, body = self.get("/s/movie/zh-Hans.m3u8?token=a%2Fb&foo=1")
        self.assertEqual(
            (status, body), (200, build_vtt_playlist(14400, "zh-Hans").encode())
        )
        self.assertEqual(headers["Content-Type"], "application/vnd.apple.mpegurl")

    def test_vtt_with_query_and_zero_offset_keeps_exact_bytes(self) -> None:
        status, headers, body = self.get("/s/movie/zh-Hans.vtt?token=1")
        self.assertEqual((status, body), (200, self.original))
        self.assertEqual(headers["Content-Type"], "text/vtt; charset=utf-8")

    def test_zero_offset_does_not_decode_vtt(self) -> None:
        self.vtt.write_bytes(self.original + b"\xff")
        self.assertEqual(self.get("/s/movie/zh-Hans.vtt")[2], self.original + b"\xff")

    def test_live_positive_and_negative_offsets_do_not_change_source_file(self) -> None:
        for offset, timing in (
            (2.5, b"00:00:03.500 --> 00:00:05.500"),
            (-2, b"00:00:00.000 --> 00:00:01.000"),
            (0, b"00:00:01.000 --> 00:00:03.000"),
        ):
            self.registry.save(
                {"umc.movie": {"key": "movie", "offset_seconds": offset}}
            )
            status, _headers, body = self.get("/s/movie/zh-Hans.vtt?avplayer=1")
            self.assertEqual(status, 200)
            self.assertIn(timing, body)
            self.assertEqual(self.vtt.read_bytes(), self.original)

    def test_same_movie_offset_applies_to_both_languages_only(self) -> None:
        (self.scratch / "movie.zh-Hant.vtt").write_bytes(self.original)
        (self.scratch / "other.zh-Hans.vtt").write_bytes(self.original)
        self.registry.save({"umc.movie": {"key": "movie", "offset_seconds": 1}})
        self.assertIn(
            b"00:00:02.000 --> 00:00:04.000", self.get("/s/movie/zh-Hant.vtt")[2]
        )
        self.assertEqual(self.get("/s/other/zh-Hans.vtt")[2], self.original)

    def test_offset_does_not_change_legacy_playlist_duration(self) -> None:
        self.registry.save({"umc.movie": {"key": "movie", "offset_seconds": 50000}})
        self.assertEqual(
            self.get("/s/movie/zh-Hans.m3u8")[2],
            build_vtt_playlist(14400, "zh-Hans").encode(),
        )

    def test_invalid_registry_uses_zero_offset(self) -> None:
        self.registry.path.write_bytes(b"{")
        self.assertEqual(self.get("/s/movie/zh-Hans.vtt")[2], self.original)

    def test_bad_routes_and_traversal_stay_404(self) -> None:
        for path in (
            "/unknown",
            "/s/movie/en.vtt",
            "/s/movie/zh-Hans.txt",
            "/s/../zh-Hans.vtt",
            "/s/%2e%2e/zh-Hans.vtt",
            "/s/movie/zh-Hans.vtt/extra",
        ):
            with self.subTest(path=path):
                status, _headers, body = self.get(path)
                self.assertEqual((status, body), (404, b"not found\n"))

    def test_missing_subtitles_keep_original_404_body(self) -> None:
        for suffix in ("vtt", "m3u8"):
            status, _headers, body = self.get(f"/s/missing/zh-Hans.{suffix}?a=b")
            self.assertEqual((status, body), (404, b"no such subtitle\n"))


class ServerSetupTests(ScratchTestCase):
    def test_production_defaults_point_to_real_store(self) -> None:
        settings = ServerConfig()
        self.assertEqual(settings.port, 17897)
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(
            str(SUBS_DIR),
            str(LIVE_DIR / "subs"),
        )
        self.assertEqual(REGISTRY_PATH, SUBS_DIR / "registry.json")
        self.assertEqual(settings.cert_path, CERT_PATH)
        self.assertEqual(settings.key_path, KEY_PATH)
        self.assertEqual(str(LOG_PATH), "/tmp/appletv-mitm.log")
        self.assertEqual(SUB_BASE, "https://127.0.0.1:17897/s")

    def test_tls_uses_existing_chain_and_defers_handshake(self) -> None:
        fake_server = Mock()
        original_socket = fake_server.socket
        with (
            patch("appletv_zhsubs.server.ssl.SSLContext") as context,
            patch(
                "appletv_zhsubs.server.ThreadingHTTPServer", return_value=fake_server
            ) as server,
        ):
            self.assertIs(create_server(), fake_server)
        server.assert_called_once()
        self.assertEqual(server.call_args.args[0], ("127.0.0.1", 17897))
        context.return_value.load_cert_chain.assert_called_once_with(
            CERT_PATH, KEY_PATH
        )
        context.return_value.wrap_socket.assert_called_once_with(
            original_socket,
            server_side=True,
            do_handshake_on_connect=False,
        )

    def test_tls_wrap_failure_closes_server(self) -> None:
        fake_server = Mock()
        with (
            patch("appletv_zhsubs.server.ssl.SSLContext") as context,
            patch(
                "appletv_zhsubs.server.ThreadingHTTPServer", return_value=fake_server
            ),
        ):
            context.return_value.wrap_socket.side_effect = OSError("TLS failure")
            with self.assertRaises(OSError):
                create_server()
        fake_server.server_close.assert_called_once()
