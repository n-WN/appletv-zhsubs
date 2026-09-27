"""Offline tests for the media-playlist pre-warmer."""

import threading
from unittest.mock import Mock

from appletv_zhsubs.log import AuditLogger
from appletv_zhsubs.warmer import (
    PlaylistWarmer,
    _asset_key,
    collect_warm_urls,
)
from tests.support import ScratchTestCase

MASTER_URL = "https://play-edge.itunes.apple.com/WebObjects/MZPlayLocal.woa/hls/subscription/playlist.m3u8?t=TOK1&a=6804782509"


def _variant(cdn: str, g: int, rung: int, token: str = "TOK1") -> str:
    return (
        f"stream/playlist.m3u8?cc=US&g={g}&cdn=vod-{cdn}-aoc.tv.apple.com"
        f"&rung={rung}&t={token}&keyInfo=KI{rung}"
    )


def _master(video_g: int = 240, audio_g: int = 32, token: str = "TOK1") -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
    for cdn in ("ap", "fa", "ak"):
        lines.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-{cdn}",NAME="English",'
            f'LANGUAGE="en",DEFAULT=YES,PATHWAY-ID="ap",'
            f'URI="{_variant(cdn, audio_g, 1, token)}"'
        )
    for cdn in ("ap", "fa", "ak"):
        for rung in (1, 2, 3):
            lines.append(
                f'#EXT-X-STREAM-INF:BANDWIDTH={rung * 1000},PATHWAY-ID="{cdn}",'
                f'AUDIO="audio-{cdn}"'
            )
            lines.append(_variant(cdn, video_g, rung, token))
    return "\n".join(lines) + "\n"


class CollectTests(ScratchTestCase):
    def test_picks_two_video_and_default_audio_per_pathway(self) -> None:
        urls = collect_warm_urls(_master(), MASTER_URL)
        self.assertEqual(len(urls), 9)
        for cdn in ("ap", "fa", "ak"):
            mine = [u for u in urls if f"vod-{cdn}-" in u]
            self.assertEqual(len(mine), 3)
            self.assertTrue(any("g=240" in u and "rung=1" in u for u in mine))
            self.assertTrue(any("g=240" in u and "rung=2" in u for u in mine))
            self.assertTrue(any("g=32" in u for u in mine))
            self.assertFalse(any("rung=3" in u for u in mine))

    def test_resolves_relative_against_master(self) -> None:
        urls = collect_warm_urls(_master(), MASTER_URL)
        base = "https://play-edge.itunes.apple.com/WebObjects/MZPlayLocal.woa/hls/subscription/stream/playlist.m3u8"
        self.assertTrue(all(u.startswith(base) for u in urls))

    def test_skips_absolute_urls_of_other_hosts(self) -> None:
        master = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=1,PATHWAY-ID="ap"\n'
            "https://vod-ap-aoc.tv.apple.com/asset/playlist.m3u8\n"
        )
        self.assertEqual(collect_warm_urls(master, MASTER_URL), [])

    def test_audio_fallback_when_no_default(self) -> None:
        master = (
            "#EXTM3U\n"
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",LANGUAGE="en",'
            f'URI="{_variant("ap", 32, 1)}"\n'
            "#EXT-X-STREAM-INF:BANDWIDTH=1\n"
            f"{_variant('ap', 240, 1)}\n"
        )
        urls = collect_warm_urls(master, MASTER_URL)
        self.assertEqual(len(urls), 2)

    def test_caps_total_urls(self) -> None:
        urls = collect_warm_urls(_master(), MASTER_URL, max_urls=4)
        self.assertEqual(len(urls), 4)

    def test_dedupes_identical_urls(self) -> None:
        master = _master() + (
            '#EXT-X-STREAM-INF:BANDWIDTH=9,PATHWAY-ID="ap"\n'
            + _variant("ap", 240, 1)
            + "\n"
        )
        urls = collect_warm_urls(master, MASTER_URL)
        self.assertEqual(len(urls), len(set(urls)))


class AssetKeyTests(ScratchTestCase):
    def test_token_params_do_not_change_key(self) -> None:
        one = _variant("ap", 240, 1, token="AAA")
        two = _variant("ap", 240, 1, token="BBB")
        base = "https://play-edge.itunes.apple.com/x/stream/"
        self.assertEqual(_asset_key(base + one), _asset_key(base + two))

    def test_encoded_token_param_is_stripped(self) -> None:
        url = "https://h/p.m3u8?mainAssetEncodedQueryParams=cc%3DUS%26t%3DSECRET%26a%3D1&g=240"
        self.assertNotIn("SECRET", _asset_key(url))
        self.assertIn("g=240", _asset_key(url))


class WarmerTests(ScratchTestCase):
    def make_warmer(self) -> tuple[PlaylistWarmer, list[str], list[float]]:
        calls: list[str] = []
        clock_values = [0.0]

        def fake_fetch(
            url: str, timeout: float, proxy: tuple[str, int]
        ) -> tuple[int, int]:
            calls.append(url)
            return 200, 1000

        warmer = PlaylistWarmer(
            fetch=fake_fetch,
            logger=Mock(spec=AuditLogger),
            ttl=60.0,
            clock=lambda: clock_values[0],
        )
        return warmer, calls, clock_values

    def drain(self, warmer: PlaylistWarmer) -> None:
        for thread in threading.enumerate():
            if thread.daemon and thread is not threading.main_thread():
                thread.join(timeout=5)

    def test_fetches_each_playlist_once_within_ttl(self) -> None:
        warmer, calls, _ = self.make_warmer()
        self.assertEqual(warmer.warm_async(_master(), MASTER_URL), 9)
        self.drain(warmer)
        self.assertEqual(warmer.warm_async(_master(), MASTER_URL), 0)
        self.assertEqual(len(calls), 9)

    def test_refetches_after_ttl(self) -> None:
        warmer, calls, clock_values = self.make_warmer()
        warmer.warm_async(_master(), MASTER_URL)
        self.drain(warmer)
        clock_values[0] = 61.0
        self.assertEqual(warmer.warm_async(_master(), MASTER_URL), 9)
        self.drain(warmer)
        self.assertEqual(len(calls), 18)

    def test_failed_fetch_is_retried_next_time(self) -> None:
        warmer, calls, _ = self.make_warmer()

        def broken(url: str, timeout: float, proxy: tuple[str, int]) -> tuple[int, int]:
            raise OSError("refused")

        warmer._fetch = broken
        warmer.warm_async(_master(), MASTER_URL)
        self.drain(warmer)
        self.assertEqual(warmer.warm_async(_master(), MASTER_URL), 9)
        self.drain(warmer)
        self.assertEqual(len(calls), 0)

    def test_inflight_urls_are_not_duplicated(self) -> None:
        gate = threading.Event()
        calls: list[str] = []

        def slow(url: str, timeout: float, proxy: tuple[str, int]) -> tuple[int, int]:
            calls.append(url)
            gate.wait(timeout=5)
            return 200, 1

        warmer = PlaylistWarmer(fetch=slow, logger=Mock(spec=AuditLogger), ttl=60.0)
        try:
            self.assertEqual(warmer.warm_async(_master(), MASTER_URL), 9)
            self.assertEqual(warmer.warm_async(_master(), MASTER_URL), 0)
        finally:
            gate.set()
        self.drain(warmer)
        self.assertEqual(len(calls), 9)

    def test_new_token_same_asset_is_not_refetched(self) -> None:
        warmer, calls, _ = self.make_warmer()
        warmer.warm_async(_master(token="TOK1"), MASTER_URL)
        self.drain(warmer)
        other = MASTER_URL.replace("TOK1", "TOK2")
        self.assertEqual(warmer.warm_async(_master(token="TOK2"), other), 0)
        self.assertEqual(len(calls), 9)
