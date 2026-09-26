"""Test timestamp offsets, cue preservation, and the legacy duration rule."""

import unittest

from appletv_zhsubs.vtt import duration_of, parse_timestamp, shift_timestamp, shift_vtt
from tests.support import ScratchTestCase, original_function


class TimestampTests(unittest.TestCase):
    def test_parse_hours_and_short_times(self) -> None:
        for text, seconds in (
            ("00:00:00.000", 0),
            ("01:02:03.456", 3723.456),
            ("02:03.456", 123.456),
            ("1:03.456", 63.456),
            ("123:00:00.001", 442800.001),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_timestamp(text), seconds)

    def test_invalid_timestamp_raises(self) -> None:
        for text in ("", "word", "1", "00:00:00,000", "00:00:00.1", "-00:00:01.000"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_timestamp(text)

    def test_positive_offset_and_hour_rollover(self) -> None:
        self.assertEqual(shift_timestamp("00:59:59.900", 0.2), "01:00:00.100")

    def test_negative_offset_and_clamp(self) -> None:
        self.assertEqual(shift_timestamp("00:00:03.100", -1.2), "00:00:01.900")
        self.assertEqual(shift_timestamp("00:00:01.000", -2), "00:00:00.000")

    def test_short_timestamp_is_formatted_with_hours(self) -> None:
        self.assertEqual(shift_timestamp("01:00.001", 1.001), "00:01:01.002")

    def test_submillisecond_offset_is_rounded(self) -> None:
        self.assertEqual(shift_timestamp("00:00:00.001", 0.0006), "00:00:00.002")
        self.assertEqual(shift_timestamp("00:00:00.001", -0.0006), "00:00:00.000")

    def test_nonfinite_offsets_raise(self) -> None:
        for offset in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                shift_vtt("WEBVTT\n\n", offset)

    def test_large_finite_negative_offset_clamps_without_overflow(self) -> None:
        self.assertEqual(shift_timestamp("00:00:01.000", -1e308), "00:00:00.000")


class CueShiftTests(unittest.TestCase):
    def test_zero_offset_keeps_all_text_exact(self) -> None:
        text = "\ufeffWEBVTT\r\n\r\ncue-id\r\n00:01.001 --> 00:02.002\r\n中文"
        self.assertEqual(shift_vtt(text), text)
        self.assertEqual(shift_vtt(text, 0.0001), text)

    def test_positive_offset_shifts_every_cue_and_keeps_settings(self) -> None:
        text = (
            "WEBVTT\n\ncue-1\n00:00:01.000 --> 00:00:02.100 align:start position:10%\n"
            "你好\n\n00:03.200 --> 00:04.300\n世界\n"
        )
        expected = (
            "WEBVTT\n\ncue-1\n00:00:02.250 --> 00:00:03.350 align:start position:10%\n"
            "你好\n\n00:00:04.450 --> 00:00:05.550\n世界\n"
        )
        self.assertEqual(shift_vtt(text, 1.25), expected)

    def test_negative_offset_clamps_start_and_end_separately(self) -> None:
        text = (
            "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nfirst\n\n"
            "00:00:00.100 --> 00:00:00.200\nsecond\n"
        )
        result = shift_vtt(text, -2)
        self.assertIn("00:00:00.000 --> 00:00:01.000", result)
        self.assertIn("00:00:00.000 --> 00:00:00.000", result)
        self.assertIn("second\n", result)

    def test_bom_crlf_whitespace_and_missing_final_newline_survive(self) -> None:
        text = "\ufeffWEBVTT\r\n\r\nid\r\n\t00:00:01.000 \t-->  00:00:02.000 align:end\r\n中文"
        self.assertEqual(
            shift_vtt(text, 1),
            "\ufeffWEBVTT\r\n\r\nid\r\n\t00:00:02.000 \t-->  00:00:03.000 align:end\r\n中文",
        )

    def test_headers_and_noncue_blocks_do_not_shift(self) -> None:
        prefix = (
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n"
            "NOTE test\n00:00:01.000 --> 00:00:02.000\n\n"
            "STYLE\n00:00:01.000 --> 00:00:02.000\n\n"
            "REGION\n00:00:01.000 --> 00:00:02.000\n\n"
        )
        result = shift_vtt(prefix + "00:00:01.000 --> 00:00:02.000\ntext\n", 1)
        self.assertTrue(result.startswith(prefix))
        self.assertTrue(result.endswith("00:00:02.000 --> 00:00:03.000\ntext\n"))

    def test_cue_text_that_looks_like_timing_stays_unchanged(self) -> None:
        text = (
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n00:00:04.000 --> 00:00:05.000\n"
        )
        result = shift_vtt(text, 1)
        self.assertTrue(result.endswith("00:00:04.000 --> 00:00:05.000\n"))

    def test_malformed_timing_lines_stay_unchanged(self) -> None:
        text = "WEBVTT\n\n00:00:01.bad --> 00:00:02.000\ntext\n"
        self.assertEqual(shift_vtt(text, 2), text)


class DurationTests(ScratchTestCase):
    def test_minimum_is_four_hours(self) -> None:
        path = self.scratch / "short.vtt"
        path.write_text("WEBVTT\n\n00:01.000 --> 00:02.000\ntext\n", encoding="utf-8")
        self.assertEqual(duration_of(path), 14400)

    def test_duration_uses_maximum_start_not_end_or_file_order(self) -> None:
        path = self.scratch / "long.vtt"
        path.write_text(
            "WEBVTT\n\n05:00:00.999 --> 08:00:00.000\nlast\n\n"
            "01:00:00.000 --> 02:00:00.000\nfirst\n",
            encoding="utf-8",
        )
        self.assertEqual(duration_of(path), 21600)

    def test_missing_file_uses_legacy_minimum(self) -> None:
        self.assertEqual(duration_of(self.scratch / "missing.vtt"), 14400)

    def test_invalid_utf8_does_not_block_duration(self) -> None:
        path = self.scratch / "invalid.vtt"
        path.write_bytes(b"WEBVTT\n\n05:00:00.000 --> 05:00:02.000\n\xff\n")
        self.assertEqual(duration_of(path), 21600)

    def test_duration_matches_live_original(self) -> None:
        original = original_function("sub_server.py", "_duration_of")
        if original is None:
            self.skipTest("Standalone original not present.")
        path = self.scratch / "sample.vtt"
        for text in (
            "",
            "WEBVTT\n\n",
            "WEBVTT\n\n01:00.000 --> 01:01.000\ntext\n",
            "WEBVTT\n\n05:00:00.900 --> 06:00:00.000\ntext\n",
        ):
            path.write_text(text, encoding="utf-8")
            self.assertEqual(duration_of(path), original(path))
