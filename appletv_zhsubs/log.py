"""Append audit records with the existing JSON-lines schema."""

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .config import LOG_PATH

type AuditEvent = Literal[
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
    "warm",
    "warm_done",
    "warm_fail",
]


@dataclass(slots=True)
class AuditLogger:
    """Write one record per line. A log failure must not stop playback."""

    path: Path = LOG_PATH
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def emit(self, event: AuditEvent, **fields: object) -> None:
        record = {"ev": event, **fields, "t": time.strftime("%Y-%m-%d %H:%M:%S")}
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except OSError:
            pass
