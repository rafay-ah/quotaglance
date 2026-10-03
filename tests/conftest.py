"""Shared test helpers: a fake HTTP client, fake home directories, fixtures."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quotaglance.net import Http, HttpError, Response
from quotaglance.providers.base import FetchContext
from quotaglance.secretstore import MemorySecretStore

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def fixture(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def load_json(*parts: str) -> Any:
    return json.loads(fixture(*parts).read_text(encoding="utf-8"))


def load_text(*parts: str) -> str:
    return fixture(*parts).read_text(encoding="utf-8")


class FakeHttp(Http):
    """Routes requests to canned responses by (METHOD, URL-prefix).

    A route value can be JSON-able data, a Response, an Exception to raise,
    or a callable ``(method, url, headers, body) -> any of those``.
    """

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        super().__init__()
        self.routes = dict(routes or {})
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, url: str, value: Any) -> None:
        self.routes[(method.upper(), url)] = value

    def request(self, method, url, *, headers=None, json_body=None, data=None, timeout=None,
                follow_redirects=True):
        method = method.upper()
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "json": json_body, "data": data,
                           "follow_redirects": follow_redirects})
        matches = [(k, v) for k, v in self.routes.items()
                   if k[0] == method and url.startswith(k[1])]
        if not matches:
            raise AssertionError(f"unexpected request {method} {url}")
        _key, value = max(matches, key=lambda kv: len(kv[0][1]))
        if callable(value) and not isinstance(value, (Response, Exception)):
            value = value(method, url, headers or {}, json_body)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, Response):
            return value
        return Response(200, url, json.dumps(value).encode())

    def last(self, url_prefix: str) -> dict[str, Any]:
        for call in reversed(self.calls):
            if call["url"].startswith(url_prefix):
                return call
        raise AssertionError(f"no call to {url_prefix}")


def http_error(status: int, body: Any = b"", headers: dict | None = None) -> HttpError:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return HttpError(status, "https://example.invalid", raw, headers or {})


class FakeRunner:
    """Stands in for subprocess.run; maps argv[0] basenames to outputs."""

    def __init__(self, outputs: dict[str, tuple[int, str, str] | Exception] | None = None) -> None:
        self.outputs = dict(outputs or {})
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        name = Path(argv[0]).name
        result = self.outputs.get(name)
        if result is None:
            raise FileNotFoundError(argv[0])
        if isinstance(result, Exception):
            raise result
        code, out, err = result
        return subprocess.CompletedProcess(argv, code, out, err)


def make_ctx(home: Path, *, http: Http | None = None, env: dict | None = None,
             secrets: dict | None = None, settings: dict | None = None,
             foreign_secrets: list | None = None,
             now: datetime = NOW, runner: FakeRunner | None = None) -> FetchContext:
    environment = {"HOME": str(home), "PATH": "/nonexistent"}
    environment.update(env or {})
    return FetchContext(
        home=home,
        env=environment,
        http=http or FakeHttp(),
        secrets=MemorySecretStore(secrets or {}, foreign_secrets),
        settings=settings or {},
        now=lambda: now,
        runner=runner or FakeRunner(),
        # Only look for CLIs inside the fake home, never on the host.
        bin_dirs=("~/.local/bin", "~/bin", "~/.npm-global/bin", "~/.bun/bin"),
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def jwt(claims: dict[str, Any]) -> str:
    import base64

    def part(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"
