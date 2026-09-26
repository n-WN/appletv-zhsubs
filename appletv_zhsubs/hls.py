"""Build HLS text without changing the production attribute or line order."""

import re
from collections.abc import Collection

from .config import LANGUAGE_NAMES, LANGUAGES, SUB_BASE


def inject_subs(
    master_text: str,
    key: str,
    langs: Collection[str],
    sub_base: str = SUB_BASE,
) -> str:
    """Add Chinese tracks to the first variant's subtitle group, as before."""
    lines = master_text.splitlines()
    group_id: str | None = None
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r'SUBTITLES="([^"]+)"', line)
            if match:
                group_id = match.group(1)
            break
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
        f'NAME="{LANGUAGE_NAMES[lang]}",LANGUAGE="{lang}",AUTOSELECT=YES,'
        f'DEFAULT=NO,FORCED=NO,URI="{sub_base}/{key}/{lang}.m3u8"'
        for lang in LANGUAGES
        if lang in langs
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
