"""Check exact rendition bytes, HLS lines, and the original implementation."""

import ast
import unittest

from appletv_zhsubs.config import SUB_BASE, TEST_MASTER
from appletv_zhsubs.hls import build_vtt_playlist, inject_subs
from tests.support import LIVE_DIR, original_function

WITH_GROUP = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",URI="en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,SUBTITLES="subs"
high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,SUBTITLES="subs"
low.m3u8
"""

WITHOUT_GROUP = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=5000000
high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000
low.m3u8
"""

EXPECTED_WITH_GROUP = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",URI="en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="简体中文",LANGUAGE="zh-Hans",AUTOSELECT=YES,DEFAULT=NO,FORCED=NO,URI="https://127.0.0.1:17897/s/wolf2013/zh-Hans.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="繁體中文",LANGUAGE="zh-Hant",AUTOSELECT=YES,DEFAULT=NO,FORCED=NO,URI="https://127.0.0.1:17897/s/wolf2013/zh-Hant.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,SUBTITLES="subs"
high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,SUBTITLES="subs"
low.m3u8
"""

EXPECTED_WITHOUT_GROUP = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="inj-subs",NAME="简体中文",LANGUAGE="zh-Hans",AUTOSELECT=YES,DEFAULT=NO,FORCED=NO,URI="https://127.0.0.1:17897/s/wolf2013/zh-Hans.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="inj-subs",NAME="繁體中文",LANGUAGE="zh-Hant",AUTOSELECT=YES,DEFAULT=NO,FORCED=NO,URI="https://127.0.0.1:17897/s/wolf2013/zh-Hant.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,SUBTITLES="inj-subs"
high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,SUBTITLES="inj-subs"
low.m3u8
"""


class InjectionTests(unittest.TestCase):
    def test_existing_group_golden_bytes(self) -> None:
        self.assertEqual(
            inject_subs(
                WITH_GROUP, "wolf2013", ["zh-Hant", "zh-Hans"], SUB_BASE
            ).encode(),
            EXPECTED_WITH_GROUP.encode(),
        )

    def test_missing_group_golden_bytes(self) -> None:
        self.assertEqual(
            inject_subs(
                WITHOUT_GROUP, "wolf2013", ["zh-Hans", "zh-Hant"], SUB_BASE
            ).encode(),
            EXPECTED_WITHOUT_GROUP.encode(),
        )

    def test_both_golden_outputs_match_live_original(self) -> None:
        original = original_function("appletv-zh-subs.py", "inject_subs")
        if original is None:
            self.skipTest(
                "Standalone original not present; golden-byte tests still run."
            )
        for master, expected in (
            (WITH_GROUP, EXPECTED_WITH_GROUP),
            (WITHOUT_GROUP, EXPECTED_WITHOUT_GROUP),
        ):
            with self.subTest(master=master):
                old = original(master, "wolf2013", ["zh-Hans", "zh-Hant"])
                self.assertIsInstance(old, str)
                self.assertEqual(old.encode("utf-8"), expected.encode("utf-8"))
                self.assertEqual(
                    inject_subs(
                        master, "wolf2013", ["zh-Hans", "zh-Hant"], SUB_BASE
                    ).encode(),
                    old.encode("utf-8"),
                )

    def test_edge_cases_match_live_original(self) -> None:
        original = original_function("appletv-zh-subs.py", "inject_subs")
        if original is None:
            self.skipTest("Standalone original not present.")
        masters = [
            WITH_GROUP,
            WITHOUT_GROUP,
            "",
            "#EXTM3U",
            "#EXTM3U\n\n",
            WITHOUT_GROUP.replace("\n", "\r\n"),
            WITH_GROUP.rstrip("\n"),
            (
                "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\none\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=2,SUBTITLES="other"\ntwo\n'
            ),
            '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,SUBTITLES=""\nx\n',
        ]
        for master in masters:
            for langs in ([], ["en"], ["zh-Hans"], ["zh-Hant"], ["zh-Hant", "zh-Hans"]):
                with self.subTest(master=master, langs=langs):
                    self.assertEqual(
                        inject_subs(master, "wolf2013", langs).encode(),
                        original(master, "wolf2013", langs).encode(),
                    )

    def test_exact_attribute_order(self) -> None:
        injected = inject_subs(WITHOUT_GROUP, "key", ["zh-Hans"])
        media = next(
            line for line in injected.splitlines() if line.startswith("#EXT-X-MEDIA:")
        )
        self.assertEqual(
            [
                attribute.split("=", 1)[0]
                for attribute in media.split(":", 1)[1].split(",")
            ],
            [
                "TYPE",
                "GROUP-ID",
                "NAME",
                "LANGUAGE",
                "AUTOSELECT",
                "DEFAULT",
                "FORCED",
                "URI",
            ],
        )

    def test_no_supported_language_preserves_original_bytes(self) -> None:
        text = WITHOUT_GROUP.replace("\n", "\r\n").rstrip()
        self.assertEqual(inject_subs(text, "key", ["en"]), text)

    def test_first_variant_controls_group_selection(self) -> None:
        text = WITH_GROUP.replace(
            'BANDWIDTH=1000000,SUBTITLES="subs"', 'BANDWIDTH=1000000,SUBTITLES="other"'
        )
        result = inject_subs(text, "key", ["zh-Hans"])
        self.assertIn('NAME="简体中文"', result)
        self.assertIn('GROUP-ID="subs",NAME="简体中文"', result)
        self.assertIn('BANDWIDTH=1000000,SUBTITLES="other"', result)

    def test_missing_first_group_updates_every_variant_as_before(self) -> None:
        text = WITHOUT_GROUP.replace(
            "BANDWIDTH=1000000", 'BANDWIDTH=1000000,SUBTITLES="other"'
        )
        result = inject_subs(text, "key", ["zh-Hans"])
        self.assertIn('SUBTITLES="other",SUBTITLES="inj-subs"', result)

    def test_no_variant_appends_renditions(self) -> None:
        result = inject_subs("#EXTM3U\n#comment", "key", ["zh-Hant"])
        self.assertTrue(result.startswith("#EXTM3U\n#comment\n#EXT-X-MEDIA:"))
        self.assertTrue(result.endswith("\n"))
        self.assertNotIn('LANGUAGE="zh-Hans"', result)

    def test_custom_base_is_used_without_normalization(self) -> None:
        result = inject_subs(
            WITHOUT_GROUP, "key", ["zh-Hans"], "https://example.test/s/"
        )
        self.assertIn('URI="https://example.test/s//key/zh-Hans.m3u8"', result)

    def test_repeated_injection_is_not_silently_deduplicated(self) -> None:
        first = inject_subs(WITHOUT_GROUP, "key", ["zh-Hans"])
        self.assertEqual(
            inject_subs(first, "key", ["zh-Hans"]).count('NAME="简体中文"'), 2
        )


class PlaylistTests(unittest.TestCase):
    def test_exact_playlist_bytes(self) -> None:
        for duration in (14400, 19800):
            for lang in ("zh-Hans", "zh-Hant"):
                with self.subTest(duration=duration, lang=lang):
                    expected = (
                        "#EXTM3U\n#EXT-X-VERSION:3\n"
                        f"#EXT-X-TARGETDURATION:{duration}\n"
                        "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
                        "#EXT-X-TIMESTAMP-MAP:LOCAL=00:00:00.000,MPEGTS=900000\n"
                        f"#EXTINF:{duration}.000,\n{lang}.vtt\n#EXT-X-ENDLIST\n"
                    )
                    self.assertEqual(
                        build_vtt_playlist(duration, lang).encode(), expected.encode()
                    )

    def test_test_master_matches_live_original_bytes(self) -> None:
        path = LIVE_DIR / "sub_server.py"
        if not path.is_file():
            self.skipTest("Standalone original not present.")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "TEST_MASTER"
                for target in node.targets
            ):
                self.assertEqual(
                    TEST_MASTER.encode(), ast.literal_eval(node.value).encode()
                )
                break
        else:
            self.fail("The original test master was not found.")
        self.assertIn("bipbop_4x3/gear1/prog_index.m3u8", TEST_MASTER)
