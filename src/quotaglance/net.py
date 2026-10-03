"""A tiny HTTP client on top of urllib (no third-party dependencies)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from quotaglance import __version__

DEFAULT_TIMEOUT = 15.0
USER_AGENT = f"QuotaGlance/{__version__} (+https://github.com/rafay-ah/quotaglance)"


class NetworkError(Exception):
    """The request never produced an HTTP response (DNS, TLS, timeout...)."""


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: bytes = b"",
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.url = url
        self.body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        super().__init__(f"HTTP {status} from {url.split('?')[0]}")

    @property
    def retry_after(self) -> float | None:
        value = self.headers.get("retry-after")
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except ValueError:
            return None

    def text(self, limit: int = 300) -> str:
        return self.body.decode("utf-8", errors="replace")[:limit]


@dataclass
class Response:
    status: int
    url: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NetworkError(f"Invalid JSON from {self.url.split('?')[0]}") from exc

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class Http:
    """Blocking HTTP client. Providers call it from worker threads."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, user_agent: str = USER_AGENT) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self._ssl = ssl.create_default_context()

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                json_body: Any = None, data: bytes | None = None,
                timeout: float | None = None) -> Response:
        all_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            all_headers.update(headers)
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            all_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=all_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout,
                                        context=self._ssl) as resp:
                return Response(resp.status, url, resp.read(), dict(resp.headers.items()))
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            raise HttpError(exc.code, url, body, dict(exc.headers.items()) if exc.headers else {}
                            ) from None
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(f"Network error: {reason}") from None

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()

    def post_json(self, url: str, body: Any = None, **kwargs: Any) -> Any:
        return self.post(url, json_body=body if body is not None else {}, **kwargs).json()
