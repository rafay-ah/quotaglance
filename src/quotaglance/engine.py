"""Fetch scheduling and the in-memory store of provider snapshots.

The engine is toolkit-agnostic: fetches run on a small thread pool, and
results are handed back through ``dispatch`` (the GUI passes a function
that hops onto the GLib main loop; the CLI just calls straight through).
All state mutation happens inside dispatched callbacks, i.e. on one thread.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from quotaglance import paths
from quotaglance.config import Config
from quotaglance.models import ProviderSnapshot, Status
from quotaglance.net import Http
from quotaglance.providers import all_providers
from quotaglance.providers.base import FetchContext, Provider, ProviderError, run_provider
from quotaglance.secretstore import SecretStore
from quotaglance.util import utcnow

log = logging.getLogger(__name__)

Dispatch = Callable[[Callable[[], None]], None]
Listener = Callable[[str | None], None]

LOCAL_REFRESH_SECONDS = 60
MAX_BACKOFF_SECONDS = 30 * 60
STALE_KEEP_SECONDS = 24 * 3600


def _direct(fn: Callable[[], None]) -> None:
    fn()


class Engine:
    def __init__(self, config: Config, secrets: SecretStore, *, http: Http | None = None,
                 providers: Iterable[Provider] | None = None, dispatch: Dispatch = _direct,
                 demo: bool = False, cache_path: Path | None = None,
                 max_workers: int = 4) -> None:
        self.config = config
        self.secrets = secrets
        self.http = http or Http()
        self.providers: dict[str, Provider] = {p.id: p for p in (providers or all_providers())}
        self.dispatch = dispatch
        self.demo = demo
        self.cache_path = cache_path or paths.cache_dir() / "snapshots.json"
        self.snapshots: dict[str, ProviderSnapshot] = {}
        self.in_flight: set[str] = set()
        self._next_due: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._listeners: dict[int, Listener] = {}
        self._next_listener = 1
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix="quotaglance-fetch")
        if not demo:
            self._load_cache()

    # -- listeners ------------------------------------------------------------

    def connect(self, listener: Listener) -> int:
        handler = self._next_listener
        self._next_listener += 1
        self._listeners[handler] = listener
        return handler

    def disconnect(self, handler: int) -> None:
        self._listeners.pop(handler, None)

    def _emit(self, provider_id: str | None) -> None:
        for listener in list(self._listeners.values()):
            try:
                listener(provider_id)
            except Exception:
                log.exception("Engine listener failed")

    # -- provider selection ---------------------------------------------------

    def ordered_providers(self) -> list[Provider]:
        order = self.config.get("provider_order") or []
        rank = {pid: i for i, pid in enumerate(order)}
        base = list(self.providers)
        return sorted(self.providers.values(),
                      key=lambda p: (rank.get(p.id, len(rank) + base.index(p.id))))

    def is_enabled(self, provider_id: str) -> bool:
        if self.demo:
            from quotaglance import demo

            return provider_id in demo.DEMO_PROVIDERS
        return self.config.provider_enabled(provider_id) is True

    def enabled_providers(self) -> list[Provider]:
        return [p for p in self.ordered_providers() if self.is_enabled(p.id)]

    def context_for(self, provider: Provider) -> FetchContext:
        return FetchContext.default(self.http, self.secrets,
                                    self.config.provider_settings(provider.id))

    def detect_all(self) -> dict[str, bool]:
        """Offline detection of which providers look set up on this machine."""
        found = {}
        for provider in self.providers.values():
            try:
                found[provider.id] = bool(provider.detect(self.context_for(provider)))
            except Exception:
                log.exception("Detection failed for %s", provider.id)
                found[provider.id] = False
        return found

    def apply_first_run(self, detected: dict[str, bool]) -> None:
        """Enable whatever was detected for providers the user never decided on."""
        for pid, present in detected.items():
            if self.config.provider_enabled(pid) is None:
                self.config.set_provider(pid, "enabled", present)
        self.config.set("first_run_complete", True)

    def start(self) -> None:
        """Kick off first-run detection (if needed) and the first refresh."""
        if self.demo or self.config.get("first_run_complete"):
            self.refresh()
            return

        def detect_then_refresh() -> None:
            detected = self.detect_all()
            def apply() -> None:
                self.apply_first_run(detected)
                self.refresh()
                self._emit(None)

            self.dispatch(apply)

        self._executor.submit(detect_then_refresh)

    # -- refresh --------------------------------------------------------------

    def interval_for(self, provider: Provider) -> int:
        if self.demo:
            return 20
        if provider.refresh_seconds:
            return min(provider.refresh_seconds, self.config.refresh_seconds)
        if provider.local_only:
            return min(LOCAL_REFRESH_SECONDS, self.config.refresh_seconds)
        return self.config.refresh_seconds

    def tick(self) -> None:
        """Refresh whatever is due. Call this every ~15-30 seconds."""
        now = time.monotonic()
        due = [p.id for p in self.enabled_providers()
               if p.id not in self.in_flight and self._next_due.get(p.id, 0) <= now]
        if due:
            self.refresh(due, force=False)

    def refresh(self, provider_ids: Iterable[str] | None = None, force: bool = True) -> None:
        ids = list(provider_ids) if provider_ids is not None else [
            p.id for p in self.enabled_providers()]
        started = False
        for pid in ids:
            provider = self.providers.get(pid)
            if provider is None or pid in self.in_flight or not self.is_enabled(pid):
                continue
            if force:
                self._failures.pop(pid, None)
            self.in_flight.add(pid)
            started = True
            ctx = self.context_for(provider)
            future = self._executor.submit(self._fetch, provider, ctx)
            future.add_done_callback(lambda f, p=provider: self._done(p, f))
        if started:
            self._emit(None)

    def _fetch(self, provider: Provider, ctx: FetchContext
               ) -> tuple[ProviderSnapshot, ProviderError | None]:
        if self.demo:
            from quotaglance import demo

            return demo.demo_snapshot(provider, ctx.now()), None
        return run_provider(provider, ctx)

    def _done(self, provider: Provider, future: Future) -> None:
        try:
            snapshot, error = future.result()
        except Exception as exc:  # should not happen: run_provider catches
            log.exception("Fetch crashed for %s", provider.id)
            snapshot = ProviderSnapshot(provider.id, Status.ERROR, message=str(exc),
                                        fetched_at=utcnow())
            error = ProviderError(str(exc))
        self.dispatch(lambda: self._apply(provider, snapshot, error))

    def _apply(self, provider: Provider, snapshot: ProviderSnapshot,
               error: ProviderError | None) -> None:
        self.in_flight.discard(provider.id)
        previous = self.snapshots.get(provider.id)
        interval = self.interval_for(provider)
        if error is None:
            self._failures.pop(provider.id, None)
            delay = float(interval)
        else:
            failures = self._failures.get(provider.id, 0) + 1
            self._failures[provider.id] = failures
            delay = min(MAX_BACKOFF_SECONDS, interval * (2 ** (failures - 1)))
            if error.retry_after:
                delay = max(delay, float(error.retry_after))
            if error.status is Status.NOT_CONFIGURED:
                delay = max(delay, float(self.config.refresh_seconds))
            snapshot = self._merge_failure(previous, snapshot, error)
            log.info("%s: %s", provider.id, error.message)
        self._next_due[provider.id] = time.monotonic() + delay
        self.snapshots[provider.id] = snapshot
        if not self.demo:
            self._save_cache()
        self._emit(provider.id)

    @staticmethod
    def _merge_failure(previous: ProviderSnapshot | None, failed: ProviderSnapshot,
                       error: ProviderError) -> ProviderSnapshot:
        """Keep showing the last good numbers (marked stale) after a failure."""
        if (previous is None or not previous.windows
                or error.status is Status.NOT_CONFIGURED or previous.fetched_at is None):
            return failed
        age = (utcnow() - previous.fetched_at).total_seconds()
        if age > STALE_KEEP_SECONDS:
            return failed
        return replace(previous, status=Status.STALE if error.transient else Status.ERROR,
                       message=error.message, hint=error.hint)

    def refresh_blocking(self, timeout: float = 60.0) -> dict[str, ProviderSnapshot]:
        """Synchronous refresh used by the command line (``--status``)."""
        futures = {}
        for provider in self.enabled_providers():
            ctx = self.context_for(provider)
            futures[provider.id] = self._executor.submit(self._fetch, provider, ctx)
        wait(list(futures.values()), timeout=timeout)
        for pid, future in futures.items():
            provider = self.providers[pid]
            if future.done():
                snapshot, error = future.result()
                self._apply(provider, snapshot, error)
        return dict(self.snapshots)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- presentation helpers -----------------------------------------------------

    def visible_snapshots(self) -> list[tuple[Provider, ProviderSnapshot | None]]:
        return [(p, self.snapshots.get(p.id)) for p in self.enabled_providers()]

    def headline(self, now: datetime | None = None, provider_id: str | None = None):
        """(provider, window) with the highest usage, for the panel and widget."""
        now = now or utcnow()
        best = None
        for provider, snap in self.visible_snapshots():
            if snap is None or (provider_id and provider.id != provider_id):
                continue
            window = snap.peak(now)
            if window is None:
                continue
            if best is None or (window.used_percent or 0) > (best[1].used_percent or 0):
                best = (provider, window)
        return best

    def last_updated(self) -> datetime | None:
        times = [s.fetched_at for s in self.snapshots.values()
                 if s.fetched_at and s.status in (Status.OK, Status.STALE, Status.ERROR)]
        return max(times) if times else None

    # -- cache ----------------------------------------------------------------

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        cutoff = utcnow() - timedelta(seconds=STALE_KEEP_SECONDS)
        for pid, data in raw.get("snapshots", {}).items():
            if pid not in self.providers or not isinstance(data, dict):
                continue
            snap = ProviderSnapshot.from_dict(data)
            if snap.fetched_at and snap.fetched_at >= cutoff and snap.windows:
                if snap.status is Status.OK:
                    snap = replace(snap, status=Status.STALE, message=None)
                self.snapshots[pid] = snap

    def _save_cache(self) -> None:
        data = {"version": 1, "snapshots": {pid: s.to_dict() for pid, s in self.snapshots.items()
                                            if s.windows}}
        try:
            paths.atomic_write_text(self.cache_path, json.dumps(data))
        except OSError as exc:
            log.debug("Could not write cache %s: %s", self.cache_path, exc)


def env_demo_enabled() -> bool:
    return os.environ.get("QUOTAGLANCE_DEMO", "").lower() in ("1", "true", "yes", "on")
