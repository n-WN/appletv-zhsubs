"""Pre-warm play-edge media playlists before the player asks for them.

play-edge generates each variant/audio media playlist on first touch; a
cold playlist trickles at ~20 kB/s and AVPlayer abandons the feature before
it starts, while the interstitial — served from a warm CDN edge — plays
fine ("ads play, movie does not"). The cache is keyed by asset, not by the
signed token: completing one fetch per representative playlist warms every
later session. Fetches run in the background through the same egress the
player traffic uses, and warming never blocks the master response.
"""

from __future__ import annotations

import http.client
import re
import ssl
import threading
import time
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

from .config import (
    LOCAL_HOST,
    PLAYEDGE_HOST,
    UPSTREAM_PORT,
    USER_AGENT,
    WARM_MAX_URLS,
    WARM_TIMEOUT,
    WARM_TTL_SECONDS,
    WARM_VIDEO_PER_PATHWAY,
)
from .log import AuditLogger

type FetchFunc = Callable[[str, float, tuple[str, int]], tuple[int, int]]

_TOKEN_PARAMS = re.compile(r"(t%3D[A-Za-z0-9%+/=_-]+|([?&])t=[^&]*)")


def fetch_via_proxy(
    url: str, timeout: float, proxy: tuple[str, int]
) -> tuple[int, int]:
    """GET *url* to completion through the egress proxy; return (status, bytes)."""
    parts = urlsplit(url)
    context = ssl.create_default_context()
    conn = http.client.HTTPSConnection(
        proxy[0], proxy[1], timeout=timeout, context=context
    )
    conn.set_tunnel(parts.hostname, parts.port or 443)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    conn.request("GET", target, headers={"User-Agent": USER_AGENT})
    response = conn.getresponse()
    total = 0
    try:
        while chunk := response.read(1 << 16):
            total += len(chunk)
    finally:
        conn.close()
    return response.status, total


def _asset_key(url: str) -> str:
    """Identify the playlist independently of the signed token params."""
    return _TOKEN_PARAMS.sub("", url)


def collect_warm_urls(
    master_text: str,
    master_url: str,
    video_per_pathway: int = WARM_VIDEO_PER_PATHWAY,
    max_urls: int = WARM_MAX_URLS,
    host: str = PLAYEDGE_HOST,
) -> list[str]:
    """Pick representative media playlists from a master playlist.

    Per CDN pathway (the `cdn` query param, PATHWAY-ID as fallback) take the
    first few video variants — the rungs a player is most likely to open —
    plus the DEFAULT audio rendition. Only playlists served by the playlist
    host benefit from warming; absolute CDN URLs (interstitials) are skipped.
    """
    videos: dict[str, list[str]] = {}
    audios: dict[str, str] = {}
    audio_fallback: dict[str, str] = {}
    lines = master_text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            uri = ""
            for follow in lines[index + 1 :]:
                if follow.strip():
                    if not follow.startswith("#"):
                        uri = follow.strip()
                    break
            if not uri:
                continue
            pathway = _cdn_of(uri) or _attr(line, "PATHWAY-ID") or "-"
            videos.setdefault(pathway, []).append(uri)
        elif line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            uri = _attr(line, "URI")
            if not uri:
                continue
            pathway = _cdn_of(uri) or _attr(line, "PATHWAY-ID") or "-"
            if "DEFAULT=YES" in line:
                audios.setdefault(pathway, uri)
            else:
                audio_fallback.setdefault(pathway, uri)
    picked: list[str] = []
    for pathway in videos.keys() | audios.keys() | audio_fallback.keys():
        picked.extend(videos.get(pathway, [])[:video_per_pathway])
        audio = audios.get(pathway) or audio_fallback.get(pathway)
        if audio:
            picked.append(audio)
    resolved: list[str] = []
    seen: set[str] = set()
    for uri in picked:
        absolute = urljoin(master_url, uri)
        if urlsplit(absolute).hostname != host:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        resolved.append(absolute)
        if len(resolved) >= max_urls:
            break
    return resolved


def _cdn_of(uri: str) -> str | None:
    match = re.search(r"[?&]cdn=([^&]+)", uri)
    return match.group(1) if match else None


def _attr(line: str, name: str) -> str | None:
    match = re.search(rf'{name}="([^"]+)"', line)
    return match.group(1) if match else None


class PlaylistWarmer:
    """Fetch cold media playlists in the background, once per asset per TTL."""

    def __init__(
        self,
        fetch: FetchFunc | None = None,
        logger: AuditLogger | None = None,
        proxy: tuple[str, int] = (LOCAL_HOST, UPSTREAM_PORT),
        ttl: float = WARM_TTL_SECONDS,
        timeout: float = WARM_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch if fetch is not None else fetch_via_proxy
        self._logger = logger if logger is not None else AuditLogger()
        self._proxy = proxy
        self._ttl = ttl
        self._timeout = timeout
        self._clock = clock
        self._warmed: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    def warm_async(self, master_text: str, master_url: str) -> int:
        """Spawn background fetches for playlists not warmed recently."""
        urls = collect_warm_urls(master_text, master_url)
        due: list[tuple[str, str]] = []
        with self._lock:
            for url in urls:
                key = _asset_key(url)
                if key in self._inflight:
                    continue
                last = self._warmed.get(key)
                if last is not None and self._clock() - last < self._ttl:
                    continue
                self._inflight.add(key)
                due.append((key, url))
        for key, url in due:
            threading.Thread(
                target=self._warm_one, args=(key, url), daemon=True
            ).start()
        if due:
            self._logger.emit("warm", count=len(due))
        return len(due)

    def _warm_one(self, key: str, url: str) -> None:
        try:
            status, nbytes = self._fetch(url, self._timeout, self._proxy)
            if 200 <= status < 400:
                with self._lock:
                    self._warmed[key] = self._clock()
            self._logger.emit(
                "warm_done",
                status=status,
                bytes=nbytes,
                url=_short(url),
            )
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            self._logger.emit("warm_fail", err=str(exc)[:160], url=_short(url))
        finally:
            with self._lock:
                self._inflight.discard(key)


def _short(url: str) -> str:
    """Log the playlist identity without the signed token."""
    parts = urlsplit(url)
    cdn = _cdn_of(parts.query) or "-"
    group = re.search(r"[?&]g=(\d+)", parts.query)
    return f"{parts.path.split('/')[-2]}/{cdn}/g={group.group(1) if group else '-'}"
