"""Check atomic registry writes, live offsets, and the JSON audit schema."""

import json
import threading
from pathlib import Path
from unittest.mock import patch

from appletv_zhsubs.log import AuditLogger
from appletv_zhsubs.registry import Registry
from tests.support import ScratchTestCase


class RegistryTests(ScratchTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = Registry(self.scratch / "registry.json")

    def test_roundtrip_keeps_legacy_json_format_and_unicode(self) -> None:
        data = {
            "umc.movie": {
                "key": "movie",
                "title": "中文",
                "year": 2013,
                "langs": ["zh-Hans"],
            }
        }
        self.registry.save(data)
        self.assertEqual(self.registry.load(), data)
        self.assertEqual(
            self.registry.path.read_bytes(),
            json.dumps(data, ensure_ascii=False, indent=1).encode(),
        )

    def test_missing_corrupt_and_nonobject_registry_are_empty(self) -> None:
        self.assertEqual(self.registry.load(), {})
        for content in (b"{", b"\xff", b"null", b"[]"):
            self.registry.path.write_bytes(content)
            self.assertEqual(self.registry.load(), {})

    def test_save_failure_keeps_original_and_removes_temp_file(self) -> None:
        self.registry.save({"umc.first": {"key": "first"}})
        original = self.registry.path.read_bytes()
        with (
            patch.object(Path, "replace", side_effect=OSError("replace failed")),
            self.assertRaises(OSError),
        ):
            self.registry.save({"umc.second": {"key": "second"}})
        self.assertEqual(self.registry.path.read_bytes(), original)
        self.assertEqual(list(self.scratch.glob(".*.tmp")), [])

    def test_atomic_save_preserves_existing_file_permissions(self) -> None:
        self.registry.save({"umc.movie": {"key": "movie"}})
        self.registry.path.chmod(0o640)
        self.registry.save({"umc.movie": {"key": "updated"}})
        self.assertEqual(self.registry.path.stat().st_mode & 0o777, 0o640)

    def test_update_reads_latest_data_and_keeps_offset(self) -> None:
        self.registry.save(
            {
                "umc.first": {"key": "first", "offset_seconds": -1.25},
                "umc.other": {"key": "other"},
            }
        )
        result = self.registry.update(
            "umc.first", {"key": "first", "langs": ["zh-Hant"]}
        )
        self.assertEqual(result["offset_seconds"], -1.25)
        self.assertIn("umc.other", self.registry.load())

    def test_offsets_default_to_zero_and_are_read_on_each_call(self) -> None:
        self.assertEqual(self.registry.offset_for_key("movie"), 0)
        self.registry.save({"umc.movie": {"key": "movie"}})
        self.assertEqual(self.registry.offset_for_key("movie"), 0)
        for offset in (-2.5, 0, 3.125):
            self.registry.save(
                {"umc.movie": {"key": "movie", "offset_seconds": offset}}
            )
            self.assertEqual(self.registry.offset_for_key("movie"), offset)
            self.assertEqual(self.registry.offset_for_key("other"), 0)

    def test_invalid_offsets_are_zero(self) -> None:
        for value in ("2", None, True, [], float("nan"), float("inf"), 10**400):
            with self.subTest(value=value):
                self.registry.path.write_text(
                    json.dumps(
                        {"umc.movie": {"key": "movie", "offset_seconds": value}}
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(self.registry.offset_for_key("movie"), 0)

    def test_readers_never_see_partial_json(self) -> None:
        self.registry.save({"umc.movie": {"key": "movie", "offset_seconds": 0}})
        stop = threading.Event()
        failures: list[Exception] = []

        def read_continuously() -> None:
            while not stop.is_set():
                try:
                    json.loads(self.registry.path.read_bytes())
                except (OSError, ValueError) as exc:
                    failures.append(exc)

        reader = threading.Thread(target=read_continuously, daemon=True)
        reader.start()
        try:
            for value in range(30):
                self.registry.save(
                    {"umc.movie": {"key": "movie", "offset_seconds": value}}
                )
        finally:
            stop.set()
            reader.join(timeout=2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(failures, [])


class AuditTests(ScratchTestCase):
    def test_all_event_names_keep_ev_t_and_fields(self) -> None:
        path = self.scratch / "audit.log"
        logger = AuditLogger(path)
        events = (
            "rewrite",
            "response",
            "inspect",
            "movie",
            "fetch",
            "fetch_ok",
            "fetch_fail",
            "inject",
            "inject_skip",
            "inject_fail",
        )
        with patch(
            "appletv_zhsubs.log.time.strftime", return_value="2026-09-27 00:00:00"
        ):
            for event in events:
                logger.emit(event, msg="中文")
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(events))
        for event, line in zip(events, lines, strict=True):
            self.assertEqual(
                line,
                json.dumps(
                    {"ev": event, "msg": "中文", "t": "2026-09-27 00:00:00"},
                    ensure_ascii=False,
                ),
            )

    def test_log_io_failure_is_ignored(self) -> None:
        AuditLogger(self.scratch / "missing" / "audit.log").emit(
            "inject_skip", why="test"
        )
