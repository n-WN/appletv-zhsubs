"""Test the SubHD flow and conversion without external requests or commands."""

import io
import itertools
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from appletv_zhsubs.config import BSDTAR, NETWORK_TIMEOUT, PROCESS_TIMEOUT, SUBHD_BASE
from appletv_zhsubs.fetcher import SubHD, SubHDFetcher, _pick_score
from appletv_zhsubs.registry import Registry
from tests.support import ScratchTestCase, original_function


class ClientTests(unittest.TestCase):
    def test_get_has_timeout_and_referer(self) -> None:
        client = SubHD()
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b"body")
        self.assertEqual(
            client._get("https://example.test/file", "https://example.test/page"),
            b"body",
        )
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Referer"), "https://example.test/page")
        self.assertEqual(
            client.opener.open.call_args.kwargs, {"timeout": NETWORK_TIMEOUT}
        )

    def test_post_has_json_timeout_and_referer(self) -> None:
        client = SubHD()
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b'{"success": true}')
        self.assertEqual(
            client._post_json("https://example.test/api", {"sid": "id"}, "ref"),
            {"success": True},
        )
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Referer"), "ref")
        self.assertEqual(json.loads(request.data), {"sid": "id"})
        self.assertEqual(
            client.opener.open.call_args.kwargs, {"timeout": NETWORK_TIMEOUT}
        )

    def test_nonobject_json_is_rejected(self) -> None:
        client = SubHD()
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b"[]")
        with self.assertRaises(TypeError):
            client._post_json("https://example.test/api", {}, "ref")

    def test_search_tags_and_dedup_match_original_rules(self) -> None:
        html = (
            "<a href='/a/one'> Movie 2013 BluRay </a><p>简体 繁体 官方字幕 SRT</p>"
            + " " * 1300
            + "<a href='/a/one'>duplicate</a>"
            + " " * 1300
            + "<a href='/a/two'>Another release</a><p>繁体 ASS</p>"
            + " " * 1300
            + "<a href='/a/help'>字幕使用简单教程</a>"
        )
        with patch.object(SubHD, "_get", return_value=html.encode()) as get:
            results = SubHD().search("Movie 2013")
        get.assert_called_once_with(f"{SUBHD_BASE}/search/Movie%202013")
        self.assertEqual(
            results,
            [
                {
                    "sid": "one",
                    "release": "Movie 2013 BluRay",
                    "langs": ["zh-Hans", "zh-Hant"],
                    "official": True,
                    "is_srt": True,
                },
                {
                    "sid": "two",
                    "release": "Another release",
                    "langs": ["zh-Hant"],
                    "official": False,
                    "is_srt": False,
                },
            ],
        )

    def test_exact_five_step_sequence(self) -> None:
        client = SubHD()
        calls = Mock()
        with (
            patch.object(client, "_get", return_value=b"") as get,
            patch.object(
                client,
                "_post_json",
                side_effect=[
                    {"success": True, "url": "/down/abc"},
                    {"success": True, "url": "https://download.test/file.rar"},
                ],
            ) as post,
        ):
            calls.attach_mock(get, "get")
            calls.attach_mock(post, "post")
            client.search("Movie 2013")
            self.assertEqual(
                client.get_file_url("abc"), "https://download.test/file.rar"
            )
        self.assertEqual(
            calls.mock_calls,
            [
                call.get(f"{SUBHD_BASE}/search/Movie%202013"),
                call.get(f"{SUBHD_BASE}/a/abc"),
                call.post(
                    f"{SUBHD_BASE}/api/sub/prepare-download",
                    {"sid": "abc"},
                    f"{SUBHD_BASE}/a/abc",
                ),
                call.get(f"{SUBHD_BASE}/down/abc", referer=f"{SUBHD_BASE}/a/abc"),
                call.post(
                    f"{SUBHD_BASE}/api/sub/down",
                    {"sid": "abc"},
                    f"{SUBHD_BASE}/down/abc",
                ),
            ],
        )

    def test_failed_prepare_stops_flow(self) -> None:
        with (
            patch.object(SubHD, "_get", return_value=b"") as get,
            patch.object(SubHD, "_post_json", return_value={"success": False}) as post,
            self.assertRaisesRegex(RuntimeError, "prepare-download failed"),
        ):
            SubHD().get_file_url("abc")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(post.call_count, 1)

    def test_invalid_final_url_is_rejected(self) -> None:
        with (
            patch.object(SubHD, "_get", return_value=b""),
            patch.object(
                SubHD,
                "_post_json",
                side_effect=[
                    {"success": True, "url": "/down/abc"},
                    {"success": True, "url": None},
                ],
            ),
            self.assertRaisesRegex(RuntimeError, "down failed"),
        ):
            SubHD().get_file_url("abc")

    def test_ranking_weights_and_live_original(self) -> None:
        original = original_function("sub_fetcher.py", "_pick_score")
        for hans, official, srt, release in itertools.product(
            (False, True),
            (False, True),
            (False, True),
            ("Movie", "Movie 2013", "Movie 2013 WEB-DL", "Movie BluRay", "Movie BDRip"),
        ):
            entry = {
                "sid": "id",
                "release": release,
                "langs": ["zh-Hans"] if hans else ["zh-Hant"],
                "official": official,
                "is_srt": srt,
            }
            expected = 4 * hans + 3 * official + 2 * srt + 2 * ("2013" in release)
            expected += int(
                any(tag in release for tag in ("WEB-DL", "BluRay", "BDRip"))
            )
            with self.subTest(entry=entry):
                self.assertEqual(_pick_score(entry, 2013), expected)
                if original is not None:
                    self.assertEqual(_pick_score(entry, 2013), original(entry, 2013))


class FetchTests(ScratchTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = Registry(self.scratch / "registry.json")
        self.client = Mock(spec=SubHD)
        self.factory = Mock(return_value=self.client)
        self.fetcher = SubHDFetcher(
            self.scratch,
            self.registry,
            self.scratch / "work",
            self.factory,
        )

    def test_registry_hit_avoids_network(self) -> None:
        entry = {"key": "movie", "langs": ["zh-Hans"], "offset_seconds": 0.25}
        self.registry.save({"umc.movie": entry})
        self.assertEqual(self.fetcher.fetch_for("Movie", 2013, "umc.movie"), entry)
        self.factory.assert_not_called()

    def test_existing_files_keep_key_and_language_order(self) -> None:
        for lang in ("zh-Hant", "zh-Hans"):
            (self.scratch / f"thewolf2013.{lang}.vtt").write_bytes(b"WEBVTT\n")
        entry = self.fetcher.fetch_for("The Wolf", 2013, "umc.movie")
        self.assertEqual(
            entry,
            {
                "key": "thewolf2013",
                "title": "The Wolf",
                "year": 2013,
                "langs": ["zh-Hans", "zh-Hant"],
            },
        )
        self.factory.assert_not_called()

    def test_nonlatin_title_keeps_movie_fallback_key(self) -> None:
        (self.scratch / "movie.zh-Hans.vtt").write_bytes(b"WEBVTT\n")
        self.assertEqual(
            self.fetcher.fetch_for("中文", None, "umc.movie")["key"], "movie"
        )

    def test_episode_key_carries_the_slot(self) -> None:
        (self.scratch / "hijack2023s01e01.zh-Hans.vtt").write_bytes(b"WEBVTT\n")
        entry = self.fetcher.fetch_for("Hijack", 2023, "umc.show", season=1, episode=1)
        self.assertEqual(entry["key"], "hijack2023s01e01")
        self.assertEqual(entry["season"], 1)
        self.assertEqual(entry["episode"], 1)
        self.factory.assert_not_called()

    def test_episode_registry_hit_requires_the_same_slot(self) -> None:
        self.registry.save(
            {
                "umc.show": {
                    "key": "hijacks01e01",
                    "langs": ["zh-Hans"],
                    "season": 1,
                    "episode": 1,
                }
            }
        )
        same = self.fetcher.fetch_for("Hijack", 2023, "umc.show", season=1, episode=1)
        self.assertEqual(same["key"], "hijacks01e01")
        self.factory.assert_not_called()

        # Another episode of the same show must not reuse the S01E01 entry.
        self.client.search.return_value = []
        with self.assertRaises(RuntimeError):
            self.fetcher.fetch_for("Hijack", 2023, "umc.show", season=1, episode=2)
        self.factory.assert_called()

    def test_episode_search_leads_with_the_slot_tag(self) -> None:
        self.client.search.return_value = []
        with self.assertRaises(RuntimeError):
            self.fetcher.fetch_for("Hijack", 2023, "umc.show", season=2, episode=5)
        self.assertEqual(self.client.search.call_args_list[0].args[0], "Hijack S02E05")

    def test_pick_score_prefers_the_exact_episode(self) -> None:
        base = {"sid": "id", "langs": ["zh-Hans"], "official": False, "is_srt": False}
        right = _pick_score(base | {"release": "Hijack.2023.S01E01.WEB-DL"}, 2023, 1, 1)
        wrong = _pick_score(base | {"release": "Hijack.2023.S01E02.WEB-DL"}, 2023, 1, 1)
        plain = _pick_score(base | {"release": "Hijack 2023 WEB-DL"}, 2023, 1, 1)
        self.assertGreater(right, plain)
        self.assertGreater(plain, wrong)
        # Movie calls are unchanged without a slot.
        self.assertEqual(
            _pick_score(base | {"release": "Hijack.2023.S01E02.WEB-DL"}, 2023),
            _pick_score(
                base | {"release": "Hijack.2023.S01E02.WEB-DL"}, 2023, None, None
            ),
        )

    def test_full_pipeline_fallback_ranking_archive_and_offset_merge(self) -> None:
        self.registry.save({"umc.movie": {"key": "movie2013", "offset_seconds": -0.5}})
        self.client.search.side_effect = [
            [],
            [
                {
                    "sid": "weak",
                    "release": "Movie",
                    "langs": ["zh-Hant"],
                    "official": False,
                    "is_srt": False,
                },
                {
                    "sid": "best",
                    "release": "Movie 2013 WEB-DL",
                    "langs": ["zh-Hans"],
                    "official": True,
                    "is_srt": True,
                },
            ],
        ]
        self.client.get_file_url.return_value = "https://download.test/file.rar"
        self.client._get.return_value = b"archive"
        messages: list[str] = []
        with patch.object(
            self.fetcher, "_extract_and_convert", return_value=["zh-Hans"]
        ) as convert:
            entry = self.fetcher.fetch_for("Movie", 2013, "umc.movie", messages.append)
        self.assertEqual(
            self.client.search.call_args_list, [call("Movie 2013"), call("Movie")]
        )
        self.client.get_file_url.assert_called_once_with("best")
        self.client._get.assert_called_once_with(
            "https://download.test/file.rar",
            referer=f"{SUBHD_BASE}/a/best",
        )
        archive = self.scratch / "work" / "subhd-best.bin"
        self.assertEqual(archive.read_bytes(), b"archive")
        convert.assert_called_once_with(
            archive, self.scratch / "work" / "subhd-x-best", "movie2013"
        )
        self.assertEqual(entry["offset_seconds"], -0.5)
        self.assertEqual(
            messages,
            [
                "subhd pick: best Movie 2013 WEB-DL ['zh-Hans']",
                "subhd file: https://download.test/file.rar",
            ],
        )

    def test_empty_search_does_not_save_registry(self) -> None:
        self.client.search.return_value = []
        with self.assertRaisesRegex(RuntimeError, "no subhd results"):
            self.fetcher.fetch_for("Movie", 2013, "umc.movie")
        self.assertFalse(self.registry.path.exists())

    def test_empty_conversion_does_not_save_registry(self) -> None:
        self.client.search.return_value = [
            {
                "sid": "id",
                "release": "Movie",
                "langs": ["zh-Hans"],
                "official": False,
                "is_srt": True,
            }
        ]
        self.client.get_file_url.return_value = "https://download.test/file.rar"
        self.client._get.return_value = b"archive"
        with (
            patch.object(self.fetcher, "_extract_and_convert", return_value=[]),
            self.assertRaisesRegex(RuntimeError, "no zh subtitle track"),
        ):
            self.fetcher.fetch_for("Movie", None, "umc.movie", lambda message: None)
        self.assertFalse(self.registry.path.exists())

    def test_extraction_language_selection_and_encoding_fallback(self) -> None:
        work = self.scratch / "extracted"
        archive = self.scratch / "archive.bin"
        archive.write_bytes(b"archive")
        commands: list[list[str]] = []

        def run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            commands.append(command)
            self.assertEqual(kwargs["timeout"], PROCESS_TIMEOUT)
            self.assertTrue(kwargs["capture_output"])
            if command[0] == BSDTAR:
                self.assertTrue(kwargs["check"])
                for name in (
                    "movie.chs.srt",
                    "movie.cht.ass",
                    "movie.en.srt",
                    "readme.txt",
                ):
                    (work / name).write_bytes(b"subtitle")
                return subprocess.CompletedProcess(command, 0)
            if command[command.index("-sub_charenc") + 1] == "UTF-8":
                return subprocess.CompletedProcess(command, 1)
            Path(command[-1]).write_bytes(b"WEBVTT\n" + b"x" * 101)
            return subprocess.CompletedProcess(command, 0)

        with patch("appletv_zhsubs.fetcher.subprocess.run", side_effect=run):
            made = self.fetcher._extract_and_convert(archive, work, "movie")
        self.assertEqual(set(made), {"zh-Hans", "zh-Hant"})
        self.assertEqual(commands[0], [BSDTAR, "xf", str(archive), "-C", str(work)])
        self.assertEqual(len(commands), 5)
        self.assertFalse((self.scratch / "movie.en.vtt").exists())
