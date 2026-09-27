"""Rewrite the Singapore region and inject tracks for the current movie."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path

from mitmproxy import http

from .config import (
    CONFIGURATION_PATH,
    COUNTRY,
    HLS_CONTENT_TYPE,
    MASTER_DUMP_DIR,
    PLAYEDGE_HOST,
    REGION,
    SELFTEST_MASTER,
    SELFTEST_PATH,
    SG_STOREFRONT,
    SUB_BASE,
    UTS_HOST,
)
from .fetcher import SubHDFetcher
from .hls import inject_subs, present_zh
from .log import AuditLogger
from .registry import Registry, RegistryEntry


@dataclass(frozen=True, slots=True)
class TitleMeta:
    """Identify one playable; season/episode are set for series only."""

    title: str
    year: int | None
    season: int | None = None
    episode: int | None = None


def _year_from_ms(value: object) -> int | None:
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value / 1000, tz=UTC).year
    return None


def _episode_meta(node: dict) -> TitleMeta | None:
    """Read the showTitle/season/episode shape used by episode playables."""
    title = node.get("showTitle") or node.get("title")
    season, episode = node.get("seasonNumber"), node.get("episodeNumber")
    if not (isinstance(title, str) and 1 < len(title) < 120):
        return None
    if not (isinstance(season, int) and isinstance(episode, int)):
        return None
    year = _year_from_ms(node.get("releaseDate"))
    return TitleMeta(title=title, year=year, season=season, episode=episode)


def _extract_meta(data: object) -> TitleMeta | None:
    """Extract title metadata from a uts-api movie or show response.

    Show pages put the playing episode under `smartEpisode` (and every
    playable's `canonicalMetadata`); movies keep title/year on `content`.
    The legacy deep search stays as the last resort for older shapes.
    """
    if not isinstance(data, dict):
        return None
    root = data.get("data")
    if not isinstance(root, dict):
        root = data
    smart = root.get("smartEpisode")
    if isinstance(smart, dict):
        meta = _episode_meta(smart)
        if meta:
            return meta
    playables = root.get("playables")
    if isinstance(playables, dict):
        for playable in playables.values():
            if isinstance(playable, dict):
                canonical = playable.get("canonicalMetadata")
                if isinstance(canonical, dict):
                    meta = _episode_meta(canonical)
                    if meta:
                        return meta
    content = root.get("content")
    if isinstance(content, dict):
        title = content.get("title")
        if isinstance(title, str) and 2 < len(title) < 120:
            year = (
                _year_from_ms(content.get("releaseDate"))
                or content.get("releaseYear")
                or content.get("year")
            )
            if isinstance(year, str) and year[:4].isdigit():
                year = int(year[:4])
            if isinstance(year, int) or content.get("type") in ("Movie", "Show"):
                return TitleMeta(
                    title=title, year=year if isinstance(year, int) else None
                )
    legacy = _deep_find_meta(data)
    if legacy:
        title, year = legacy
        return TitleMeta(title=title, year=year)
    return None


def _deep_find_meta(node: object) -> tuple[str, int | None] | None:
    """Return the first usable title and year in the original traversal order."""
    if isinstance(node, dict):
        title = node.get("title") or node.get("name")
        year = node.get("releaseYear") or node.get("year")
        if isinstance(title, str) and 2 < len(title) < 120:
            try:
                return title, int(str(year)[:4]) if year else None
            except (TypeError, ValueError):
                return title, None
        for value in node.values():
            hit = _deep_find_meta(value)
            if hit:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = _deep_find_meta(value)
            if hit:
                return hit
    return None


@dataclass(frozen=True, slots=True)
class CurrentMovie:
    """Keep one immutable playback snapshot under the addon lock."""

    umc: str
    key: str
    langs: tuple[str, ...]

    @classmethod
    def from_entry(cls, umc: str, entry: RegistryEntry) -> CurrentMovie:
        return cls(umc, entry["key"], tuple(entry.get("langs", [])))


def _entry_matches(entry: RegistryEntry, meta: TitleMeta | None) -> bool:
    """Trust a registry hit only when it names the same title slot.

    Movie UMCs are title-unique, so an entry without episode data is always
    safe to reuse. A show UMC covers every episode, so the stored season and
    episode must equal the ones the page is playing; when the episode is
    unknown (meta missing) an episode entry is never safe to reuse.
    """
    if meta is None or meta.episode is None:
        return not entry.get("episode")
    return entry.get("season") == meta.season and entry.get("episode") == meta.episode


class AppleTVZhSubs:
    """Connect the request hooks, registry, background fetcher, and HLS writer."""

    def __init__(
        self,
        registry: Registry | None = None,
        fetcher: SubHDFetcher | None = None,
        logger: AuditLogger | None = None,
        master_dir: Path = MASTER_DUMP_DIR,
    ) -> None:
        self.registry = registry if registry is not None else Registry()
        self.fetcher = (
            fetcher if fetcher is not None else SubHDFetcher(registry=self.registry)
        )
        self.logger = logger if logger is not None else AuditLogger()
        self.master_dir = master_dir
        self.current: CurrentMovie | None = None
        self.lock = threading.Lock()

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        path = flow.request.path.split("?", 1)[0]
        if host == UTS_HOST:
            self._uts_request(flow, path)
        elif (
            host == PLAYEDGE_HOST
            and path == SELFTEST_PATH
            and flow.request.method == "GET"
        ):
            flow.response = http.Response.make(
                200,
                SELFTEST_MASTER.encode("utf-8"),
                {"Content-Type": HLS_CONTENT_TYPE},
            )

    def _uts_request(self, flow: http.HTTPFlow, path: str) -> None:
        query = flow.request.query
        changed: dict[str, list[str]] = {}
        if path == CONFIGURATION_PATH:
            for name, target in (("region", REGION), ("country", COUNTRY)):
                if name in query and query[name] != target:
                    changed[name] = [query[name], target]
                    query[name] = target
            if "sfh" in query:
                new = re.sub(r"\d{6}", SG_STOREFRONT, query["sfh"], count=1)
                if new != query["sfh"]:
                    changed["sfh"] = [query["sfh"], new]
                    query["sfh"] = new
        flow.metadata["zh_changed"] = changed
        match = re.fullmatch(r"/uts/v3/(?:movies|shows)/(umc\.[a-z0-9.]+)", path)
        if match:
            flow.metadata["zh_umc"] = match.group(1)

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.response is None:
            return
        host = flow.request.pretty_host
        path = flow.request.path.split("?", 1)[0]
        if host == UTS_HOST:
            self._uts_response(flow, path)
        elif host == PLAYEDGE_HOST:
            self._playedge_response(flow, path)

    def _uts_response(self, flow: http.HTTPFlow, path: str) -> None:
        if flow.response is None or "zh_changed" not in flow.metadata:
            return
        umc = flow.metadata.get("zh_umc")
        if umc:
            self._on_movie_meta(flow, umc)
        if path == CONFIGURATION_PATH or flow.metadata.get("zh_changed"):
            self.logger.emit(
                "response",
                path=path,
                status=flow.response.status_code,
                bytes=len(flow.response.raw_content or b""),
                rewritten=bool(flow.metadata.get("zh_changed")),
            )

    def _on_movie_meta(self, flow: http.HTTPFlow, umc: str) -> None:
        entry = self.registry.load().get(umc)
        if entry is not None and entry.get("langs") and not entry.get("episode"):
            # Movie entries are UMC-unique: reuse them without reading the body.
            with self.lock:
                self.current = CurrentMovie.from_entry(umc, entry)
            self.logger.emit(
                "movie",
                umc=umc,
                key=entry["key"],
                langs=entry.get("langs", []),
                src="registry",
            )
            return
        meta: TitleMeta | None = None
        if flow.response is not None and flow.response.status_code == 200:
            try:
                meta = _extract_meta(json.loads(flow.response.raw_content or b"{}"))
            except ValueError:
                meta = None
        if entry is not None and entry.get("langs") and _entry_matches(entry, meta):
            with self.lock:
                self.current = CurrentMovie.from_entry(umc, entry)
            self.logger.emit(
                "movie",
                umc=umc,
                key=entry["key"],
                langs=entry.get("langs", []),
                src="registry",
            )
            return
        # A new or unknown title must never inherit the previous title's
        # tracks; clear first, then refill only when this title is ready.
        with self.lock:
            self.current = None
        if meta is None:
            self.logger.emit("movie", umc=umc, src="unparsed")
            return
        self.logger.emit(
            "movie",
            umc=umc,
            title=meta.title,
            year=meta.year,
            season=meta.season,
            episode=meta.episode,
            src="parsed",
        )
        threading.Thread(
            target=self._fetch_movie, args=(meta, umc), daemon=True
        ).start()

    def _fetch_movie(self, meta: TitleMeta, umc: str) -> None:
        try:
            entry = self.fetcher.fetch_for(
                meta.title,
                meta.year,
                umc,
                season=meta.season,
                episode=meta.episode,
                log=lambda message: self.logger.emit("fetch", msg=message),
            )
            with self.lock:
                self.current = CurrentMovie.from_entry(umc, entry)
            self.logger.emit(
                "fetch_ok",
                umc=umc,
                key=entry["key"],
                langs=entry.get("langs", []),
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
            HTTPException,
        ) as exc:
            self.logger.emit("fetch_fail", umc=umc, err=str(exc))

    def _playedge_response(self, flow: http.HTTPFlow, path: str) -> None:
        if (
            flow.response is None
            or flow.request.method != "GET"
            or not path.endswith(".m3u8")
        ):
            return
        text = flow.response.text or ""
        if not text.startswith("#EXTM3U") or "#EXT-X-STREAM-INF" not in text:
            return
        try:
            self.master_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%H%M%S")
            (self.master_dir / f"master-{stamp}.m3u8").write_text(
                text, encoding="utf-8"
            )
            (self.master_dir / f"master-{stamp}.url").write_text(
                flow.request.pretty_url,
                encoding="utf-8",
            )
        except OSError:
            pass
        with self.lock:
            current = self.current
        if current is None and path == SELFTEST_PATH:
            current = self._selftest_movie()
        if current is None or not current.key:
            self.logger.emit("inject_skip", why="no-current-movie", path=path[:80])
            return
        try:
            new_text = inject_subs(text, current.key, current.langs, SUB_BASE)
        except ValueError as exc:
            self.logger.emit("inject_fail", err=str(exc))
            return
        if new_text != text:
            flow.response.text = new_text
            self.logger.emit(
                "inject",
                key=current.key,
                langs=sorted(set(current.langs) - present_zh(text)),
                path=path[:80],
                status=flow.response.status_code,
            )

    def _selftest_movie(self) -> CurrentMovie | None:
        """Let the self-test exercise injection even before any real playback."""
        for entry in reversed(list(self.registry.load().values())):
            if entry.get("langs"):
                return CurrentMovie("umc.selftest", entry["key"], tuple(entry["langs"]))
        return None


addons = [AppleTVZhSubs()]
