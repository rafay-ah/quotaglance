"""Provider framework.

A provider turns some data source (local log files, a CLI, an HTTP API)
into a :class:`ProviderSnapshot`. Providers are plain Python and never touch
GTK, so they can be unit-tested with fixtures and used from the CLI.

Parsing lives in pure functions (``parse_*``) that take decoded JSON or
text, which keeps the network/file plumbing thin and the logic testable.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, Status, UsageWindow
from quotaglance.net import Http, HttpError, NetworkError
from quotaglance.secretstore import SecretStore
from quotaglance.util import utcnow

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------
# Errors


class ProviderError(Exception):
    """A fetch failed in a way worth showing to the user."""

    status = Status.ERROR

    def __init__(self, message: str, hint: str | None = None, *, transient: bool = False,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.transient = transient
        self.retry_after = retry_after


class NotConfigured(ProviderError):
    """No credentials or local data were found for this provider."""

    status = Status.NOT_CONFIGURED


class AuthError(ProviderError):
    """Credentials exist but were rejected or have expired."""


def from_http_error(exc: HttpError, *, login_hint: str | None = None,
                    service: str | None = None) -> ProviderError:
    """Map an HTTP failure onto a friendly, actionable error."""
    name = service or _("the service")
    if exc.status in (401, 403):
        return AuthError(_("Sign-in expired or was rejected ({status})").format(status=exc.status),
                         login_hint)
    if exc.status == 404:
        return ProviderError(_("Usage endpoint not found (404). {name} may have changed its API.")
                             .format(name=name))
    if exc.status == 429:
        return ProviderError(_("Rate limited by {name}; will retry later").format(name=name),
                             transient=True, retry_after=exc.retry_after)
    if exc.status >= 500:
        return ProviderError(_("{name} is having trouble ({status})").format(
            name=name, status=exc.status), transient=True)
    return ProviderError(_("Unexpected response ({status})").format(status=exc.status))


# --------------------------------------------------------------------------
# Settings exposed in Preferences


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: str  # "secret" | "choice" | "text" | "switch"
    title: str
    subtitle: str = ""
    choices: tuple[tuple[str, str], ...] = ()  # (value, label)
    default: Any = None
    env: tuple[str, ...] = ()  # environment variables that can supply a secret
    placeholder: str = ""


def api_key_setting(env: Sequence[str], title: str | None = None,
                    subtitle: str | None = None) -> SettingSpec:
    return SettingSpec(
        key="api_key",
        kind="secret",
        title=title or _("API key"),
        subtitle=subtitle or _("Stored in GNOME Keyring. Also read from {env}.").format(
            env=", ".join(f"${name}" for name in env)),
        env=tuple(env),
    )


# --------------------------------------------------------------------------
# Fetch context


Runner = Callable[..., subprocess.CompletedProcess]

_EXTRA_BIN_DIRS = (
    "~/.local/bin", "~/bin", "~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin",
    "~/.cargo/bin", "~/.deno/bin", "~/.yarn/bin", "~/.local/share/pnpm",
    "~/.opencode/bin", "~/.amp/bin", "~/.kiro/bin", "~/.factory/bin",
    "/usr/local/bin", "/snap/bin", "/usr/bin",
)


def _default_runner(argv: Sequence[str], timeout: float | None = None,
                    idle_timeout: float | None = None, **kwargs: Any
                    ) -> subprocess.CompletedProcess:
    if not idle_timeout:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False,
                              timeout=timeout, **kwargs)
    return _run_with_idle_cutoff(list(argv), timeout or 30.0, idle_timeout, **kwargs)


def _run_with_idle_cutoff(argv: list[str], timeout: float, idle: float, **kwargs: Any
                          ) -> subprocess.CompletedProcess:
    """Run a CLI that may keep a TUI alive after printing: stop once it goes quiet."""
    import os
    import signal
    import threading
    import time

    kwargs.pop("capture_output", None)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, **kwargs)
    chunks: dict[str, list[bytes]] = {"out": [], "err": []}
    last_output = [0.0]

    def pump(stream, key: str) -> None:
        for block in iter(lambda: stream.read1(4096), b""):
            chunks[key].append(block)
            last_output[0] = time.monotonic()

    threads = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    killed = False
    while proc.poll() is None:
        now = time.monotonic()
        quiet = last_output[0] and now - last_output[0] > idle
        if now > deadline or quiet:
            killed = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(2)
            if not quiet and not last_output[0]:
                raise subprocess.TimeoutExpired(argv, timeout)
            break
        time.sleep(0.05)
    for thread in threads:
        thread.join(1)
    out = b"".join(chunks["out"]).decode("utf-8", errors="replace")
    err = b"".join(chunks["err"]).decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(argv, 0 if killed else proc.returncode, out, err)


@dataclass
class FetchContext:
    home: Path
    env: Mapping[str, str]
    http: Http
    secrets: SecretStore
    settings: dict[str, Any] = field(default_factory=dict)
    now: Callable[[], datetime] = utcnow
    runner: Runner = _default_runner
    bin_dirs: tuple[str, ...] = _EXTRA_BIN_DIRS

    @classmethod
    def default(cls, http: Http, secrets: SecretStore, settings: dict[str, Any] | None = None
                ) -> FetchContext:
        return cls(home=Path.home(), env=dict(os.environ), http=http, secrets=secrets,
                   settings=settings or {})

    def path(self, value: str) -> Path:
        """Expand ``~`` against this context's home (lets tests use a fake home)."""
        if value.startswith("~/"):
            return self.home / value[2:]
        if value == "~":
            return self.home
        return Path(value)

    def env_path(self, name: str, default: str) -> Path:
        value = self.env.get(name)
        return self.path(value) if value else self.path(default)

    def xdg(self, name: str, default: str) -> Path:
        value = self.env.get(name)
        if value and os.path.isabs(value):
            return Path(value)
        return self.path(default)

    @property
    def config_home(self) -> Path:
        return self.xdg("XDG_CONFIG_HOME", "~/.config")

    @property
    def data_home(self) -> Path:
        return self.xdg("XDG_DATA_HOME", "~/.local/share")

    def secret(self, key: str = "api_key", env: Sequence[str] = (),
               provider: str | None = None) -> str | None:
        """Keyring first (what the user typed in Preferences), then env vars."""
        value = None
        if provider:
            value = self.secrets.lookup(provider, key)
        if value:
            return value.strip()
        for name in env:
            value = self.env.get(name)
            if value and value.strip():
                return value.strip()
        return None

    def which(self, name: str) -> str | None:
        """Find a CLI even when the desktop session PATH lacks user bin dirs."""
        found = shutil.which(name, path=self.env.get("PATH"))
        if found:
            return found
        candidates: list[str] = []
        for directory in self.bin_dirs:
            candidates.append(str(self.path(directory) / name))
        nvm = sorted(glob.glob(str(self.path("~/.nvm/versions/node/*/bin") / name)),
                     reverse=True)
        candidates.extend(nvm)
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def run(self, argv: Sequence[str], timeout: float = 20.0,
            extra_env: Mapping[str, str] | None = None,
            idle_timeout: float | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        env.update({"NO_COLOR": "1", "TERM": "dumb", "CI": "1"})
        if extra_env:
            env.update(extra_env)
        extra = {"idle_timeout": idle_timeout} if idle_timeout else {}
        try:
            return self.runner(list(argv), timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                               **extra)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(_("`{cmd}` timed out").format(cmd=" ".join(argv[:3])),
                                transient=True) from exc
        except OSError as exc:
            raise ProviderError(_("Could not run `{cmd}`: {err}").format(
                cmd=argv[0], err=exc.strerror or exc)) from exc


# --------------------------------------------------------------------------
# Provider base class


class Provider:
    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    short: ClassVar[str] = ""  # 1-2 letter monogram for the badge
    color: ClassVar[str] = "#6b7280"  # badge colour
    category: ClassVar[str] = "agents"  # agents | editors | api | media
    homepage: ClassVar[str] = ""
    source_summary: ClassVar[str] = ""  # one line: where the numbers come from
    setup_hint: ClassVar[str] = ""  # what to do when nothing is found
    local_only: ClassVar[bool] = False  # never touches the network
    refresh_seconds: ClassVar[int | None] = None  # override for cheap local sources
    settings: ClassVar[tuple[SettingSpec, ...]] = ()

    def detect(self, ctx: FetchContext) -> bool:
        """Cheap, offline check: does this machine look set up for the provider?"""
        return False

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotImplementedError

    # -- helpers for subclasses ---------------------------------------------

    def snapshot(self, ctx: FetchContext, windows: list[UsageWindow], *,
                 plan: str | None = None, account: str | None = None,
                 source: str | None = None, observed_at: datetime | None = None,
                 message: str | None = None, status: Status = Status.OK) -> ProviderSnapshot:
        return ProviderSnapshot(
            provider_id=self.id,
            status=status,
            windows=windows,
            plan=plan,
            account=account,
            source=source,
            message=message,
            fetched_at=ctx.now(),
            observed_at=observed_at,
        )

    def api_key(self, ctx: FetchContext) -> str | None:
        for spec in self.settings:
            if spec.kind == "secret" and spec.key == "api_key":
                return ctx.secret("api_key", spec.env, provider=self.id)
        return None

    def require_api_key(self, ctx: FetchContext) -> str:
        key = self.api_key(ctx)
        if not key:
            raise NotConfigured(_("No API key set"), self.setup_hint or None)
        return key


def run_provider(provider: Provider, ctx: FetchContext
                 ) -> tuple[ProviderSnapshot, ProviderError | None]:
    """Run ``provider.fetch`` and turn any failure into a snapshot."""
    try:
        return provider.fetch(ctx), None
    except ProviderError as exc:
        error = exc
    except HttpError as exc:
        error = from_http_error(exc, service=provider.name)
    except NetworkError as exc:
        error = ProviderError(str(exc), transient=True)
    except Exception as exc:  # parsing surprises must not crash the app
        error = ProviderError(_("Unexpected error: {err}").format(err=exc))
    snap = ProviderSnapshot(
        provider_id=provider.id,
        status=error.status,
        message=error.message,
        hint=error.hint,
        fetched_at=ctx.now(),
    )
    return snap, error
