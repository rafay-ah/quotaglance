"""API keys and tokens, stored in GNOME Keyring through libsecret.

QuotaGlance never writes secrets to disk itself. Each item is stored under
the schema ``io.github.rafay_ah.QuotaGlance`` with ``provider`` and ``key``
attributes, so it shows up readably in Seahorse ("Passwords and Keys").
"""

from __future__ import annotations

import logging
import threading
import time

from quotaglance import APP_ID

log = logging.getLogger(__name__)

# When the Secret Service doesn't answer, every libsecret call blocks for the
# D-Bus timeout (25 s). After one such failure, stop asking for a while.
OUTAGE_PAUSE = 300.0
_OUTAGE_DOMAINS = ("g-io-error-quark", "g-dbus-error-quark")


def is_outage(exc: Exception) -> bool:
    """True when the Secret Service itself is unreachable (not a missing item)."""
    domain = getattr(exc, "domain", None)
    if isinstance(domain, str):
        return domain in _OUTAGE_DOMAINS
    return "org.freedesktop.secrets" in str(exc) or "Timeout was reached" in str(exc)


class SecretStore:
    """Interface shared by the keyring store and the in-memory test store."""

    available = False

    def lookup(self, provider: str, key: str = "api_key") -> str | None:
        raise NotImplementedError

    def store(self, provider: str, key: str, value: str, label: str) -> bool:
        raise NotImplementedError

    def clear(self, provider: str, key: str = "api_key") -> bool:
        raise NotImplementedError

    def search(self, attributes: dict[str, str]) -> list[tuple[dict[str, str], str]]:
        """Find items stored by *other* apps (e.g. Zed) by attribute match.

        Returns (attributes, secret) pairs. Read-only.
        """
        return []


class MemorySecretStore(SecretStore):
    available = True

    def __init__(self, values: dict[tuple[str, str], str] | None = None,
                 foreign: list[tuple[dict[str, str], str]] | None = None) -> None:
        self._values = dict(values or {})
        self._foreign = list(foreign or [])

    def lookup(self, provider: str, key: str = "api_key") -> str | None:
        return self._values.get((provider, key))

    def store(self, provider: str, key: str, value: str, label: str) -> bool:
        self._values[(provider, key)] = value
        return True

    def clear(self, provider: str, key: str = "api_key") -> bool:
        return self._values.pop((provider, key), None) is not None

    def search(self, attributes: dict[str, str]) -> list[tuple[dict[str, str], str]]:
        return [(attrs, secret) for attrs, secret in self._foreign
                if all(attrs.get(k) == v for k, v in attributes.items())]


class KeyringSecretStore(SecretStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], str | None] = {}
        self._down_until = 0.0
        try:
            import gi

            gi.require_version("Secret", "1")
            from gi.repository import Secret
        except (ImportError, ValueError) as exc:
            log.warning("libsecret is unavailable (%s); API keys cannot be stored", exc)
            self._secret = None
            self._schema = None
            return
        self._secret = Secret
        self._schema = Secret.Schema.new(
            APP_ID,
            Secret.SchemaFlags.NONE,
            {
                "provider": Secret.SchemaAttributeType.STRING,
                "key": Secret.SchemaAttributeType.STRING,
            },
        )
        self.available = True

    @property
    def responding(self) -> bool:
        return time.monotonic() >= self._down_until

    def _failed(self, what: str, exc: Exception) -> None:
        if not is_outage(exc):
            log.warning("Keyring %s failed: %s", what, exc)
            return
        if self.responding:
            log.warning("GNOME Keyring is not responding (%s); trying again in %d minutes",
                        exc, OUTAGE_PAUSE // 60)
        self._down_until = time.monotonic() + OUTAGE_PAUSE

    def lookup(self, provider: str, key: str = "api_key") -> str | None:
        if not self.available:
            return None
        with self._lock:
            if (provider, key) in self._cache:
                return self._cache[(provider, key)]
        if not self.responding:
            return None
        try:
            value = self._secret.password_lookup_sync(
                self._schema, {"provider": provider, "key": key}, None)
        except Exception as exc:  # locked keyring, no Secret Service, ...
            self._failed(f"lookup for {provider}/{key}", exc)
            return None
        with self._lock:
            self._cache[(provider, key)] = value
        return value

    def store(self, provider: str, key: str, value: str, label: str) -> bool:
        if not self.available:
            return False
        try:
            ok = self._secret.password_store_sync(
                self._schema, {"provider": provider, "key": key},
                self._secret.COLLECTION_DEFAULT, label, value, None)
        except Exception as exc:
            log.error("Keyring store failed for %s/%s: %s", provider, key, exc)
            return False
        self._down_until = 0.0  # it answered
        with self._lock:
            self._cache[(provider, key)] = value
        return bool(ok)

    def clear(self, provider: str, key: str = "api_key") -> bool:
        if not self.available:
            return False
        try:
            removed = self._secret.password_clear_sync(
                self._schema, {"provider": provider, "key": key}, None)
        except Exception as exc:
            log.error("Keyring clear failed for %s/%s: %s", provider, key, exc)
            return False
        with self._lock:
            self._cache.pop((provider, key), None)
        return bool(removed)

    def search(self, attributes: dict[str, str]) -> list[tuple[dict[str, str], str]]:
        if not self.available or not self.responding:
            return []
        Secret = self._secret
        schema = Secret.Schema.new(
            "org.freedesktop.Secret.Generic", Secret.SchemaFlags.DONT_MATCH_NAME,
            {name: Secret.SchemaAttributeType.STRING for name in attributes})
        flags = Secret.SearchFlags.ALL | Secret.SearchFlags.UNLOCK | Secret.SearchFlags.LOAD_SECRETS
        try:
            items = Secret.password_search_sync(schema, attributes, flags, None)
        except Exception as exc:
            self._failed("search", exc)
            return []
        results = []
        for item in items or []:
            try:
                value = item.retrieve_secret_sync(None)
            except Exception:
                value = None
            if value is None:
                continue
            text = value.get_text() if hasattr(value, "get_text") else None
            if text is None:
                raw = value.get()
                text = bytes(raw).decode("utf-8", errors="replace") if raw is not None else None
            if text:
                results.append((dict(item.get_attributes() or {}), text))
        return results

    def forget_cache(self) -> None:
        with self._lock:
            self._cache.clear()
