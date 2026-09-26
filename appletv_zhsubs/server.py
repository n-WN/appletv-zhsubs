"""Serve the existing HLS routes over local HTTPS with live cue offsets."""

import re
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import urlsplit

from .config import HLS_CONTENT_TYPE, TEST_MASTER, VTT_CONTENT_TYPE, ServerConfig
from .hls import build_vtt_playlist
from .registry import Registry
from .vtt import duration_of, shift_vtt


class Handler(BaseHTTPRequestHandler):
    """Serve only the fixed health, test-master, and Chinese subtitle routes."""

    protocol_version = "HTTP/1.1"
    settings: ClassVar[ServerConfig] = ServerConfig()

    def setup(self) -> None:
        self.request.settimeout(self.settings.request_timeout)
        super().setup()

    def log_message(self, fmt: str, *args: object) -> None:
        try:
            with self.settings.log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"{time.strftime('%H:%M:%S')} {self.address_string()} {fmt % args}\n"
                )
        except OSError:
            pass

    def _send(self, code: int, body: str | bytes, content_type: str) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        # AVPlayer appends query parameters to both playlists and VTT requests.
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send(200, "ok\n", "text/plain")
            return
        if path == "/t/master.m3u8":
            self._send(200, TEST_MASTER, HLS_CONTENT_TYPE)
            return
        match = re.fullmatch(
            r"/s/([A-Za-z0-9_-]+)/(zh-Hans|zh-Hant)\.(m3u8|vtt)",
            path,
        )
        if match is None:
            self._send(404, "not found\n", "text/plain")
            return
        key, lang, kind = match.groups()
        vtt_path = self.settings.subs_dir / f"{key}.{lang}.vtt"
        if not vtt_path.is_file():
            self._send(404, "no such subtitle\n", "text/plain")
            return
        if kind == "vtt":
            try:
                data = vtt_path.read_bytes()
            except OSError:
                self._send(404, "no such subtitle\n", "text/plain")
                return
            offset = Registry(self.settings.registry_path).offset_for_key(key)
            if offset:
                data = shift_vtt(data.decode("utf-8", "replace"), offset).encode(
                    "utf-8"
                )
            self._send(200, data, VTT_CONTENT_TYPE)
            return
        self._send(
            200,
            build_vtt_playlist(duration_of(vtt_path), lang),
            HLS_CONTENT_TYPE,
        )


def create_server(settings: ServerConfig | None = None) -> ThreadingHTTPServer:
    """Load the existing leaf certificate; do not create or change certificates."""
    if settings is None:
        settings = ServerConfig()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(settings.cert_path, settings.key_path)

    class ConfiguredHandler(Handler):
        """Bind this server's paths without changing the default handler."""

    ConfiguredHandler.settings = settings
    server = ThreadingHTTPServer((settings.host, settings.port), ConfiguredHandler)
    try:
        # Defer each TLS handshake to its worker, where setup sets a timeout.
        server.socket = context.wrap_socket(
            server.socket,
            server_side=True,
            do_handshake_on_connect=False,
        )
    except (OSError, ValueError):
        server.server_close()
        raise
    return server


def main() -> None:
    settings = ServerConfig()
    with create_server(settings) as server:
        with settings.log_path.open("a", encoding="utf-8") as stream:
            stream.write("sub-server up\n")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
