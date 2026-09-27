"""Find SubHD tracks, download an archive, and convert Chinese tracks to VTT."""

import http.cookiejar
import json
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypedDict, cast

from .config import (
    BSDTAR,
    DEFAULT_FETCH_TITLE,
    DEFAULT_FETCH_UMC,
    DEFAULT_FETCH_YEAR,
    FFMPEG,
    LANGUAGES,
    NETWORK_TIMEOUT,
    PROCESS_TIMEOUT,
    SUBHD_BASE,
    SUBS_DIR,
    SUBTITLE_ENCODINGS,
    USER_AGENT,
    WORK_DIR,
)
from .registry import Registry, RegistryEntry


class SearchEntry(TypedDict):
    """Keep the fields used by the production ranking function."""

    sid: str
    release: str
    langs: list[str]
    official: bool
    is_srt: bool


class SubHD:
    """Keep cookies for the five search, detail, and download API steps."""

    def __init__(self) -> None:
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def _get(self, url: str, referer: str | None = None) -> bytes:
        request = urllib.request.Request(url)
        if referer:
            request.add_header("Referer", referer)
        with self.opener.open(request, timeout=NETWORK_TIMEOUT) as response:
            return response.read()

    def _post_json(
        self,
        url: str,
        payload: dict[str, str],
        referer: str,
    ) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Referer": referer},
        )
        with self.opener.open(request, timeout=NETWORK_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8", "replace"))
        if not isinstance(result, dict):
            raise TypeError("SubHD returned a non-object JSON response.")
        return cast(dict[str, object], result)

    def search(self, title: str) -> list[SearchEntry]:
        html = self._get(f"{SUBHD_BASE}/search/{urllib.parse.quote(title)}").decode(
            "utf-8", "replace"
        )
        entries: list[SearchEntry] = []
        seen: set[str] = set()
        for match in re.finditer(
            r"href='(/a/[A-Za-z0-9]+)'[^>]*>([^<]{2,120}?)</a>",
            html,
        ):
            sid_path, release = match.group(1), match.group(2).strip()
            if not release or release == "字幕使用简单教程":
                continue
            context = html[match.end() : match.end() + 1200]
            context_text = re.sub(r"<[^>]+>", " ", context)
            langs: list[str] = []
            if "简体" in context_text:
                langs.append("zh-Hans")
            if "繁体" in context_text:
                langs.append("zh-Hant")
            sid = sid_path.split("/")[-1]
            if sid in seen:
                continue
            seen.add(sid)
            entries.append(
                {
                    "sid": sid,
                    "release": release,
                    "langs": langs,
                    "official": "官方字幕" in context_text,
                    "is_srt": "SRT" in context_text,
                }
            )
        return entries

    def get_file_url(self, sid: str) -> str:
        detail_page = f"{SUBHD_BASE}/a/{sid}"
        self._get(detail_page)
        prepared = self._post_json(
            f"{SUBHD_BASE}/api/sub/prepare-download",
            {"sid": sid},
            detail_page,
        )
        down_path = prepared.get("url")
        if (
            not prepared.get("success")
            or not isinstance(down_path, str)
            or not down_path.startswith("/down/")
        ):
            raise RuntimeError(f"prepare-download failed: {prepared}")
        down_page = f"{SUBHD_BASE}{down_path}"
        self._get(down_page, referer=detail_page)
        result = self._post_json(
            f"{SUBHD_BASE}/api/sub/down",
            {"sid": sid},
            down_page,
        )
        url = result.get("url")
        if (
            not result.get("success")
            or not isinstance(url, str)
            or not url.startswith("http")
        ):
            raise RuntimeError(f"down failed: {result}")
        return url


def _pick_score(
    entry: SearchEntry,
    year: int | str | None,
    season: int | None = None,
    episode: int | None = None,
) -> int:
    """Keep the original weights and release-name checks.

    Series releases get a large bonus for the exact SxxEyy tag and a penalty
    for a tag that names a different episode, so a season page cannot pick a
    neighbour episode by accident.
    """
    score = 0
    if "zh-Hans" in entry["langs"]:
        score += 4
    if entry["official"]:
        score += 3
    if entry["is_srt"]:
        score += 2
    if year and str(year) in entry["release"]:
        score += 2
    if re.search(r"blu-?ray|bdrip|web-?dl|webrip", entry["release"], re.IGNORECASE):
        score += 1
    if season and episode:
        release = entry["release"].lower()
        wanted = rf"s0*{season}e0*{episode}(?!\d)"
        if re.search(wanted, release):
            score += 6
        else:
            other = re.search(r"s(\d{1,2})e(\d{1,2})(?!\d)", release)
            if other and (int(other.group(1)), int(other.group(2))) != (
                season,
                episode,
            ):
                score -= 8
    return score


class SubHDFetcher:
    """Serialize fetches and update the registry only after tracks exist."""

    _lock = threading.Lock()

    def __init__(
        self,
        subs_dir: Path = SUBS_DIR,
        registry: Registry | None = None,
        work_dir: Path = WORK_DIR,
        client_factory: Callable[[], SubHD] = SubHD,
    ) -> None:
        self.subs_dir = subs_dir
        self.registry = (
            registry if registry is not None else Registry(subs_dir / "registry.json")
        )
        self.work_dir = work_dir
        self.client_factory = client_factory

    def _extract_and_convert(self, archive: Path, workdir: Path, key: str) -> list[str]:
        workdir.mkdir(parents=True, exist_ok=True)
        self.subs_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [BSDTAR, "xf", str(archive), "-C", str(workdir)],
            check=True,
            capture_output=True,
            timeout=PROCESS_TIMEOUT,
        )
        made: list[str] = []
        for root, _dirs, files in workdir.walk():
            for filename in files:
                low = filename.lower()
                if not low.endswith((".srt", ".ass", ".ssa")):
                    continue
                lang: str | None = None
                if re.search(r"\.(chs|gb|sc|zh[-_.]?hans|simp)\.", low):
                    lang = "zh-Hans"
                elif re.search(r"\.(cht|big5|tc|zh[-_.]?hant|trad)\.", low):
                    lang = "zh-Hant"
                elif re.search(r"chs|简体", low):
                    lang = "zh-Hans"
                elif re.search(r"cht|繁体", low):
                    lang = "zh-Hant"
                if lang is None:
                    continue
                destination = self.subs_dir / f"{key}.{lang}.vtt"
                source = root / filename
                for encoding in SUBTITLE_ENCODINGS:
                    result = subprocess.run(
                        [
                            FFMPEG,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-y",
                            "-sub_charenc",
                            encoding,
                            "-i",
                            str(source),
                            str(destination),
                        ],
                        check=False,
                        capture_output=True,
                        timeout=PROCESS_TIMEOUT,
                    )
                    if result.returncode == 0 and destination.stat().st_size > 100:
                        break
                else:
                    continue
                if lang not in made:
                    made.append(lang)
        return made

    def fetch_for(
        self,
        title: str,
        year: int | str | None,
        umc: str,
        log: Callable[[str], None] = print,
        *,
        season: int | None = None,
        episode: int | None = None,
    ) -> RegistryEntry:
        with self._lock:
            existing = self.registry.load().get(umc)
            if existing is not None:
                if season and episode:
                    slot_match = (
                        existing.get("season") == season
                        and existing.get("episode") == episode
                    )
                else:
                    slot_match = not existing.get("episode")
            else:
                slot_match = False
            if existing and existing.get("langs") and slot_match:
                return existing
            key = re.sub(r"[^A-Za-z0-9]+", "", title.lower())[:24] or "movie"
            if year:
                key += str(year)
            if season and episode:
                key += f"s{season:02d}e{episode:02d}"
            have = [
                lang
                for lang in LANGUAGES
                if (self.subs_dir / f"{key}.{lang}.vtt").exists()
            ]
            if not have:
                client = self.client_factory()
                queries = []
                if season and episode:
                    queries.append(f"{title} S{season:02d}E{episode:02d}")
                if year:
                    queries.append(f"{title} {year}")
                queries.append(title)
                entries: list[SearchEntry] = []
                for query in queries:
                    entries = client.search(query)
                    if entries:
                        break
                if not entries:
                    raise RuntimeError(f"no subhd results for {title}")
                entries.sort(
                    key=lambda entry: _pick_score(entry, year, season, episode),
                    reverse=True,
                )
                best = entries[0]
                log(f"subhd pick: {best['sid']} {best['release'][:60]} {best['langs']}")
                url = client.get_file_url(best["sid"])
                log(f"subhd file: {url}")
                self.work_dir.mkdir(parents=True, exist_ok=True)
                archive = self.work_dir / f"subhd-{best['sid']}.bin"
                archive.write_bytes(
                    client._get(url, referer=f"{SUBHD_BASE}/a/{best['sid']}")
                )
                have = self._extract_and_convert(
                    archive,
                    self.work_dir / f"subhd-x-{best['sid']}",
                    key,
                )
            if not have:
                raise RuntimeError("no zh subtitle track extracted")
            entry: RegistryEntry = {
                "key": key,
                "title": title,
                "year": year,
                "langs": have,
            }
            if season is not None:
                entry["season"] = season
            if episode is not None:
                entry["episode"] = episode
            return self.registry.update(umc, entry)


def main(argv: Sequence[str] | None = None) -> None:
    """Keep the original command-line defaults for a manual fetch."""
    args = list(sys.argv[1:] if argv is None else argv)
    title = args[0] if args else DEFAULT_FETCH_TITLE
    year = args[1] if len(args) > 1 else DEFAULT_FETCH_YEAR
    umc = args[2] if len(args) > 2 else DEFAULT_FETCH_UMC
    print(SubHDFetcher().fetch_for(title, year, umc))


if __name__ == "__main__":
    main()
