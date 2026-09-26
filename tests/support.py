"""Keep test writes inside this tree and provide a small mitmproxy stub."""

from __future__ import annotations

import ast
import re
import sys
import tempfile
import types
import unittest
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT.parent


class ScratchTestCase(unittest.TestCase):
    """Do not let tests write to the real registry, logs, or temporary paths."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix=".test-", dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.scratch = Path(directory.name)


def original_function(filename: str, name: str) -> Callable[..., object] | None:
    """Compile only one original function, with no imports or file writes."""
    path = LIVE_DIR / filename
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        ),
        None,
    )
    if function is None:
        return None
    isolated = ast.Module(body=[function], type_ignores=[])
    namespace: dict[str, object] = {
        "re": re,
        "SUB_BASE": "https://127.0.0.1:17897/s",
    }
    exec(  # noqa: S102 -- intentional: run the legacy function in an isolated namespace for byte-comparison tests
        compile(isolated, str(path), "exec"), namespace)
    return cast(Callable[..., object], namespace[name])


@dataclass
class FakeRequest:
    pretty_host: str
    path: str
    method: str = "GET"
    query: dict[str, str] = field(default_factory=dict)

    @property
    def pretty_url(self) -> str:
        return f"https://{self.pretty_host}{self.path}"


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    @property
    def raw_content(self) -> bytes:
        return self.content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    @text.setter
    def text(self, value: str) -> None:
        self.content = value.encode("utf-8")

    @classmethod
    def make(
        cls,
        status_code: int,
        content: bytes,
        headers: dict[str, str],
    ) -> FakeResponse:
        return cls(status_code, content, headers)


@dataclass
class FakeFlow:
    request: FakeRequest
    response: FakeResponse | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def install_mitmproxy_stub() -> None:
    """Install the stub before importing the addon, even if mitmproxy exists."""
    module = types.ModuleType("mitmproxy")
    http_module = types.ModuleType("mitmproxy.http")
    http_module.HTTPFlow = FakeFlow
    http_module.Response = FakeResponse
    module.http = http_module
    sys.modules["mitmproxy"] = module
    sys.modules["mitmproxy.http"] = http_module
