"""Build HLS text without changing the production attribute or line order."""

import re
from collections.abc import Collection

from .config import LANGUAGE_NAMES, LANGUAGE_TAGS, LANGUAGES, SUB_BASE


def _norm_zh(language: str) -> str | None:
    """Map a LANGUAGE value to our track ids; Cantonese (yue) stays separate."""
    low = language.strip().lower()
    if low.startswith("yue"):
        return None
    if "hans" in low or low in {"zh-cn", "zh-sg", "zh-chs"}:
        return "zh-Hans"
    if "hant" in low or low in {"zh-tw", "zh-hk", "zh-cht"}:
        return "zh-Hant"
    return None


def present_zh(master_text: str, group_id: str | None = None) -> set[str]:
    """Return the normalized Chinese languages the subtitle group already has.

    Apple marks Mandarin tracks as cmn-Hans/cmn-Hant, so a plain "zh" search
    misses them; every Hans/Hant spelling counts here.
    """
    found: set[str] = set()
    for line in master_text.splitlines():
        if not line.startswith("#EXT-X-MEDIA:") or "TYPE=SUBTITLES" not in line:
            continue
        if group_id is not None and f'GROUP-ID="{group_id}"' not in line:
            continue
        match = re.search(r'LANGUAGE="([^"]+)"', line)
        if match:
            norm = _norm_zh(match.group(1))
            if norm:
                found.add(norm)
    return found


def inject_subs(
    master_text: str,
    key: str,
    langs: Collection[str],
    sub_base: str = SUB_BASE,
) -> str:
    """Add Chinese tracks to the first variant's subtitle group, as before.

    Idempotent, and it never duplicates a language the master already carries
    (official cmn-Hans/cmn-Hant tracks included).
    """
    if sub_base in master_text:
        return master_text
    lines = master_text.splitlines()
    group_id: str | None = None
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r'SUBTITLES="([^"]+)"', line)
            if match:
                group_id = match.group(1)
            break
    present = present_zh(master_text, group_id)
    if group_id is None:
        group_id = "inj-subs"
        lines = [
            line + ',SUBTITLES="inj-subs"'
            if line.startswith("#EXT-X-STREAM-INF:")
            else line
            for line in lines
        ]
    renditions = [
        f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="{group_id}",'
        f'NAME="{LANGUAGE_NAMES[lang]}",LANGUAGE="{LANGUAGE_TAGS[lang]}",AUTOSELECT=YES,'
        f'DEFAULT=NO,FORCED=NO,URI="{sub_base}/{key}/{lang}.m3u8"'
        for lang in LANGUAGES
        if lang in langs and lang not in present
    ]
    if not renditions:
        return master_text
    output: list[str] = []
    inserted = False
    for line in lines:
        if not inserted and line.startswith("#EXT-X-STREAM-INF:"):
            output.extend(renditions)
            inserted = True
        output.append(line)
    if not inserted:
        output.extend(renditions)
    return "\n".join(output) + "\n"


def build_vtt_playlist(duration: int, lang: str) -> str:
    """Wrap one VTT file in the exact production HLS VOD format."""
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{duration:d}\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-TIMESTAMP-MAP:LOCAL=00:00:00.000,MPEGTS=900000\n"
        f"#EXTINF:{duration:d}.000,\n"
        f"{lang}.vtt\n"
        "#EXT-X-ENDLIST\n"
    )
