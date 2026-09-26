"""Test addon triggers with a mitmproxy stub installed before its import."""

import json
import runpy
import subprocess
import sys
from http.client import HTTPException
from unittest.mock import Mock, patch

from tests.support import (
    ROOT,
    FakeFlow,
    FakeRequest,
    FakeResponse,
    ScratchTestCase,
    install_mitmproxy_stub,
    original_function,
)

install_mitmproxy_stub()

from appletv_zhsubs.addon import AppleTVZhSubs, CurrentMovie, _deep_find_meta
from appletv_zhsubs.config import (
    CONFIGURATION_PATH,
    PLAYEDGE_HOST,
    SELFTEST_MASTER,
    SELFTEST_PATH,
    UTS_HOST,
)
from appletv_zhsubs.fetcher import SubHDFetcher
from appletv_zhsubs.hls import inject_subs
from appletv_zhsubs.log import AuditLogger
from appletv_zhsubs.registry import Registry


class AddonTests(ScratchTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = Registry(self.scratch / "registry.json")
        self.fetcher = Mock(spec=SubHDFetcher)
        self.logger = Mock(spec=AuditLogger)
        self.addon = AppleTVZhSubs(
            self.registry,
            self.fetcher,
            self.logger,
            self.scratch / "masters",
        )

    def flow(
        self,
        path: str,
        host: str = UTS_HOST,
        status: int = 200,
        body: bytes = b"{}",
        method: str = "GET",
    ) -> FakeFlow:
        return FakeFlow(FakeRequest(host, path, method), FakeResponse(status, body))

    def test_region_rewrite_keeps_original_metadata_and_only_first_storefront(
        self,
    ) -> None:
        flow = self.flow(CONFIGURATION_PATH + "?region=us")
        flow.request.query = {
            "region": "us",
            "country": "US",
            "sfh": "a143441-b123456",
            "other": "1",
        }
        self.addon.request(flow)
        self.assertEqual(
            flow.request.query,
            {
                "region": "sg",
                "country": "sg",
                "sfh": "a143464-b123456",
                "other": "1",
            },
        )
        self.assertEqual(
            flow.metadata["zh_changed"],
            {
                "region": ["us", "sg"],
                "country": ["US", "sg"],
                "sfh": ["a143441-b123456", "a143464-b123456"],
            },
        )
        self.addon.response(flow)
        self.logger.emit.assert_called_once_with(
            "response",
            path=CONFIGURATION_PATH,
            status=200,
            bytes=2,
            rewritten=True,
        )

    def test_missing_query_fields_are_not_added(self) -> None:
        flow = self.flow(CONFIGURATION_PATH)
        self.addon.request(flow)
        self.assertEqual(flow.request.query, {})
        self.assertEqual(flow.metadata["zh_changed"], {})

    def test_other_uts_paths_and_hosts_are_not_rewritten(self) -> None:
        for host, path in ((UTS_HOST, "/other"), ("example.test", CONFIGURATION_PATH)):
            flow = self.flow(path, host)
            flow.request.query = {"region": "us"}
            self.addon.request(flow)
            self.assertEqual(flow.request.query, {"region": "us"})

    def test_movies_and_shows_set_umc_metadata(self) -> None:
        for kind in ("movies", "shows"):
            flow = self.flow(f"/uts/v3/{kind}/umc.cmc.abc123?sf=1")
            self.addon.request(flow)
            self.assertEqual(flow.metadata["zh_umc"], "umc.cmc.abc123")
        flow = self.flow("/uts/v3/movies/umc.BAD")
        self.addon.request(flow)
        self.assertNotIn("zh_umc", flow.metadata)

    def test_registry_hit_sets_movie_for_all_statuses_without_reading_body(
        self,
    ) -> None:
        self.registry.save({"umc.movie": {"key": "movie", "langs": ["zh-Hans"]}})

        class UnreadableResponse(FakeResponse):
            @property
            def raw_content(self) -> bytes:
                raise AssertionError("A registry hit must not read the response body.")

        for status in (200, 304, 404, 500):
            with self.subTest(status=status):
                self.addon.current = None
                flow = self.flow("/uts/v3/movies/umc.movie", status=status)
                flow.response = UnreadableResponse(status)
                self.addon.request(flow)
                self.addon.response(flow)
                self.assertEqual(
                    self.addon.current, CurrentMovie("umc.movie", "movie", ("zh-Hans",))
                )
                self.logger.emit.assert_called_with(
                    "movie",
                    umc="umc.movie",
                    key="movie",
                    langs=["zh-Hans"],
                    src="registry",
                )
        self.fetcher.fetch_for.assert_not_called()

    def test_registry_miss_does_not_parse_non200_body(self) -> None:
        current = CurrentMovie("umc.old", "old", ("zh-Hant",))
        self.addon.current = current
        with patch(
            "appletv_zhsubs.addon.json.loads",
            side_effect=AssertionError("must not parse"),
        ):
            for status in (201, 304, 403, 500):
                flow = self.flow("/uts/v3/movies/umc.movie", status=status)
                self.addon.request(flow)
                self.addon.response(flow)
        self.assertIs(self.addon.current, current)
        self.logger.emit.assert_not_called()

    def test_response_without_request_metadata_is_ignored(self) -> None:
        self.addon.response(self.flow("/uts/v3/movies/umc.movie"))
        self.logger.emit.assert_not_called()

    def test_metadata_parser_keeps_original_traversal_and_year_rules(self) -> None:
        original = original_function("appletv-zh-subs.py", "_deep_find_meta")
        examples = [
            ({"title": "Movie", "releaseYear": "2013-12-01"}, ("Movie", 2013)),
            ({"title": "", "name": "Name", "year": 1999}, ("Name", 1999)),
            (
                {"title": "Movie", "year": "bad", "child": {"title": "Other"}},
                ("Movie", None),
            ),
            (
                {"title": "ab", "child": [{"name": "Nested", "year": 2004}]},
                ("Nested", 2004),
            ),
            ([{}, {"title": "First"}, {"title": "Second"}], ("First", None)),
            ({"title": "x" * 120}, None),
            ({"title": "x" * 119}, ("x" * 119, None)),
            ({"title": "Movie", "releaseYear": 0, "year": 2013}, ("Movie", 2013)),
            (None, None),
        ]
        for body, expected in examples:
            with self.subTest(body=body):
                self.assertEqual(_deep_find_meta(body), expected)
                if original is not None:
                    self.assertEqual(_deep_find_meta(body), original(body))

    def test_parsed_200_starts_daemon_fetch(self) -> None:
        flow = self.flow(
            "/uts/v3/movies/umc.movie",
            body=json.dumps({"data": [{"title": "Movie", "year": 2013}]}).encode(),
        )
        self.addon.request(flow)
        with patch("appletv_zhsubs.addon.threading.Thread") as thread:
            self.addon.response(flow)
        thread.assert_called_once_with(
            target=self.addon._fetch_movie,
            args=("Movie", 2013, "umc.movie"),
            daemon=True,
        )
        thread.return_value.start.assert_called_once()
        self.logger.emit.assert_called_once_with(
            "movie",
            umc="umc.movie",
            title="Movie",
            year=2013,
            src="parsed",
        )

    def test_invalid_body_logs_unparsed_without_fetch(self) -> None:
        for body in (b"{", b"\xff", b"{}", b"null"):
            flow = self.flow("/uts/v3/movies/umc.movie", body=body)
            self.addon.request(flow)
            self.addon.response(flow)
            self.logger.emit.assert_called_with(
                "movie", umc="umc.movie", src="unparsed"
            )
        self.fetcher.fetch_for.assert_not_called()

    def test_fetch_success_sets_current_and_keeps_audit_fields(self) -> None:
        self.fetcher.fetch_for.return_value = {
            "key": "movie",
            "langs": ["zh-Hans", "zh-Hant"],
        }
        self.addon._fetch_movie("Movie", 2013, "umc.movie")
        self.assertEqual(
            self.addon.current,
            CurrentMovie("umc.movie", "movie", ("zh-Hans", "zh-Hant")),
        )
        self.logger.emit.assert_called_with(
            "fetch_ok",
            umc="umc.movie",
            key="movie",
            langs=["zh-Hans", "zh-Hant"],
        )
        callback = self.fetcher.fetch_for.call_args.kwargs["log"]
        callback("message")
        self.logger.emit.assert_called_with("fetch", msg="message")

    def test_expected_fetch_failures_keep_current_movie(self) -> None:
        current = CurrentMovie("umc.old", "old", ())
        self.addon.current = current
        for failure in (
            OSError("network"),
            ValueError("JSON"),
            RuntimeError("no results"),
            subprocess.TimeoutExpired("ffmpeg", 120),
            HTTPException("HTTP"),
        ):
            self.fetcher.fetch_for.side_effect = failure
            self.addon._fetch_movie("Movie", 2013, "umc.movie")
            self.assertIs(self.addon.current, current)
            self.logger.emit.assert_called_with(
                "fetch_fail", umc="umc.movie", err=str(failure)
            )

    def test_injection_requires_a_current_movie_with_a_key(self) -> None:
        for current in (None, CurrentMovie("umc.movie", "", ("zh-Hans",))):
            self.addon.current = current
            flow = self.flow(
                "/master.m3u8?q=1", PLAYEDGE_HOST, body=SELFTEST_MASTER.encode()
            )
            self.addon.response(flow)
            self.assertEqual(flow.response.text, SELFTEST_MASTER)
            self.logger.emit.assert_called_with(
                "inject_skip", why="no-current-movie", path="/master.m3u8"
            )

    def test_injection_keeps_original_dump_and_does_not_gate_on_status(self) -> None:
        self.addon.current = CurrentMovie("umc.movie", "movie", ("zh-Hans",))
        flow = self.flow(
            "/master.m3u8?q=1", PLAYEDGE_HOST, 503, SELFTEST_MASTER.encode()
        )
        self.addon.response(flow)
        self.assertEqual(
            flow.response.text, inject_subs(SELFTEST_MASTER, "movie", ["zh-Hans"])
        )
        dump = next((self.scratch / "masters").glob("*.m3u8"))
        self.assertEqual(dump.read_bytes(), SELFTEST_MASTER.encode())
        self.assertEqual(dump.with_suffix(".url").read_text(), flow.request.pretty_url)
        self.logger.emit.assert_called_once_with(
            "inject",
            key="movie",
            langs=["zh-Hans"],
            path="/master.m3u8",
            status=503,
        )

    def test_injection_gates_match_original(self) -> None:
        self.addon.current = CurrentMovie("umc.movie", "movie", ("zh-Hans",))
        for host, path, method, body in (
            ("example.test", "/master.m3u8", "GET", SELFTEST_MASTER),
            (PLAYEDGE_HOST, "/master.m3u8", "POST", SELFTEST_MASTER),
            (PLAYEDGE_HOST, "/master.txt", "GET", SELFTEST_MASTER),
            (PLAYEDGE_HOST, "/master.m3u8", "GET", "\n" + SELFTEST_MASTER),
            (PLAYEDGE_HOST, "/variant.m3u8", "GET", "#EXTM3U\n#EXTINF:10,\nx.ts\n"),
        ):
            flow = self.flow(path, host, body=body.encode(), method=method)
            self.addon.response(flow)
            self.assertEqual(flow.response.text, body)
        self.logger.emit.assert_not_called()

    def test_empty_languages_do_not_emit_inject(self) -> None:
        self.addon.current = CurrentMovie("umc.movie", "movie", ())
        flow = self.flow("/master.m3u8", PLAYEDGE_HOST, body=SELFTEST_MASTER.encode())
        self.addon.response(flow)
        self.assertEqual(flow.response.text, SELFTEST_MASTER)
        self.logger.emit.assert_not_called()

    def test_injection_failure_does_not_modify_master(self) -> None:
        self.addon.current = CurrentMovie("umc.movie", "movie", ("zh-Hans",))
        flow = self.flow("/master.m3u8", PLAYEDGE_HOST, body=SELFTEST_MASTER.encode())
        with patch(
            "appletv_zhsubs.addon.inject_subs", side_effect=ValueError("bad master")
        ):
            self.addon.response(flow)
        self.assertEqual(flow.response.text, SELFTEST_MASTER)
        self.logger.emit.assert_called_once_with("inject_fail", err="bad master")

    def test_selftest_route_with_query_uses_normal_injection_hook(self) -> None:
        self.addon.current = CurrentMovie("umc.movie", "movie", ("zh-Hans",))
        flow = FakeFlow(FakeRequest(PLAYEDGE_HOST, SELFTEST_PATH + "?test=1"))
        self.addon.request(flow)
        self.assertEqual(flow.response.text, SELFTEST_MASTER)
        self.assertEqual(
            flow.response.headers["Content-Type"], "application/vnd.apple.mpegurl"
        )
        self.addon.response(flow)
        self.assertEqual(
            flow.response.text, inject_subs(SELFTEST_MASTER, "movie", ["zh-Hans"])
        )

    def test_selftest_without_current_movie_does_not_inject(self) -> None:
        flow = FakeFlow(FakeRequest(PLAYEDGE_HOST, SELFTEST_PATH))
        self.addon.request(flow)
        self.addon.response(flow)
        self.assertEqual(flow.response.text, SELFTEST_MASTER)

    def test_selftest_does_not_intercept_other_hosts_or_methods(self) -> None:
        for host, method in (("example.test", "GET"), (PLAYEDGE_HOST, "POST")):
            flow = FakeFlow(FakeRequest(host, SELFTEST_PATH, method))
            self.addon.request(flow)
            self.assertIsNone(flow.response)

    def test_loader_exports_addons_without_installed_mitmproxy(self) -> None:
        with patch.object(sys, "path", list(sys.path)):
            loaded = runpy.run_path(str(ROOT / "appletv-zh-subs.py"))
        self.assertEqual(len(loaded["addons"]), 1)
        self.assertIsInstance(loaded["addons"][0], AppleTVZhSubs)
