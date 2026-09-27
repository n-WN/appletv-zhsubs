"""Read registry snapshots and replace the JSON file atomically."""

import json
import math
import os
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from .config import REGISTRY_PATH


class RegistryEntry(TypedDict):
    """Describe a subtitle set. An absent offset means zero seconds."""

    key: str
    title: NotRequired[str]
    year: NotRequired[int | str | None]
    season: NotRequired[int | None]
    episode: NotRequired[int | None]
    langs: NotRequired[list[str]]
    offset_seconds: NotRequired[float]


type RegistryData = dict[str, RegistryEntry]


@dataclass(slots=True)
class Registry:
    """Use a unique temporary file next to the destination for each save."""

    path: Path = REGISTRY_PATH
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def load(self) -> RegistryData:
        try:
            with self._lock, self.path.open(encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        return cast(RegistryData, value)

    def save(self, data: RegistryData) -> None:
        """Do not expose a partial JSON document to another process."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                mode = stat.S_IMODE(self.path.stat().st_mode)
            except FileNotFoundError:
                mode = None
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    if mode is not None:
                        os.fchmod(stream.fileno(), mode)
                    json.dump(data, stream, ensure_ascii=False, indent=1)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def update(self, umc: str, entry: RegistryEntry) -> RegistryEntry:
        """Merge a completed fetch with the latest entry, including its offset."""
        with self._lock:
            data = self.load()
            merged = cast(RegistryEntry, {**data.get(umc, {}), **entry})
            data[umc] = merged
            self.save(data)
            return merged

    def offset_for_key(self, key: str) -> float:
        """Read a finite numeric offset. Invalid or absent values mean zero."""
        for entry in self.load().values():
            if not isinstance(entry, dict) or entry.get("key") != key:
                continue
            value = entry.get("offset_seconds", 0)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return 0.0
            try:
                offset = float(value)
            except OverflowError:
                return 0.0
            return offset if math.isfinite(offset) else 0.0
        return 0.0
