"""Parse cue times and apply a signed offset without changing cue text."""

import math
import re
from decimal import Decimal
from pathlib import Path

from .config import MIN_PLAYLIST_DURATION, PLAYLIST_MARGIN

_TIMESTAMP = r"(?:(\d+):)?(\d{1,2}):(\d{2})\.(\d{3})"
_CUE_LINE = re.compile(
    rf"^(?P<indent>[ \t]*)(?P<start>{_TIMESTAMP})"
    rf"(?P<arrow>[ \t]+-->[ \t]+)(?P<end>{_TIMESTAMP})(?=[ \t\r\n]|$)"
)


def _milliseconds(timestamp: str) -> int:
    match = re.fullmatch(_TIMESTAMP, timestamp)
    if match is None:
        raise ValueError(f"Invalid WebVTT timestamp: {timestamp!r}")
    hours, minutes, seconds, milliseconds = match.groups()
    return (int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 + int(
        milliseconds
    )


def parse_timestamp(timestamp: str) -> float:
    """Return seconds for an HH:MM:SS.mmm or MM:SS.mmm timestamp."""
    return _milliseconds(timestamp) / 1000


def _format_milliseconds(milliseconds: int) -> str:
    seconds, remainder = divmod(max(0, milliseconds), 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{remainder:03d}"


def _offset_milliseconds(offset_seconds: float) -> int:
    if not math.isfinite(offset_seconds):
        raise ValueError("The subtitle offset must be finite.")
    # Decimal avoids binary rounding drift and overflow when scaling a large float.
    return round(Decimal(str(offset_seconds)) * 1000)


def shift_timestamp(timestamp: str, offset_seconds: float) -> str:
    """Shift to the nearest millisecond. Clamp negative results to zero."""
    return _format_milliseconds(
        _milliseconds(timestamp) + _offset_milliseconds(offset_seconds)
    )


def shift_vtt(text: str, offset_seconds: float = 0.0) -> str:
    """Shift cue starts and ends; keep headers, settings, and line endings."""
    offset_ms = _offset_milliseconds(offset_seconds)
    if offset_ms == 0:
        return text
    output: list[str] = []
    block_line = 0
    ignored = False
    for line in text.splitlines(keepends=True):
        content = line.strip().lstrip("\ufeff")
        if not content:
            block_line = 0
            ignored = False
        else:
            if block_line == 0:
                ignored = content in (
                    "WEBVTT", "NOTE", "STYLE", "REGION",
                ) or content.startswith(("WEBVTT ", "WEBVTT\t", "NOTE ", "NOTE\t"))
            # A cue timing line follows either a blank line or one cue ID.
            if not ignored and block_line < 2:
                match = _CUE_LINE.match(line)
                if match:
                    start = _format_milliseconds(
                        _milliseconds(match["start"]) + offset_ms
                    )
                    end = _format_milliseconds(_milliseconds(match["end"]) + offset_ms)
                    line = (
                        f"{match['indent']}{start}{match['arrow']}{end}"
                        + line[match.end() :]
                    )
                    block_line = 2
            block_line += 1
        output.append(line)
    return "".join(output)


def duration_of(vtt_path: Path) -> int:
    """Keep the legacy duration: maximum cue start plus 1h, at least 4h."""
    last = 0.0
    try:
        text = vtt_path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(rf"({_TIMESTAMP})\s*-->", text):
            last = max(last, parse_timestamp(match.group(1)))
    except OSError:
        pass
    return max(int(last) + PLAYLIST_MARGIN, MIN_PLAYLIST_DURATION)
