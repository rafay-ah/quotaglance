import threading

from quotaglance import secretstore
from quotaglance.secretstore import KeyringSecretStore, is_outage


class FakeGLibError(Exception):
    def __init__(self, domain, message):
        super().__init__(message)
        self.domain = domain


class FakeSecret:
    def __init__(self, error=None, value="sk-test"):
        self.error = error
        self.value = value
        self.calls = 0

    def password_lookup_sync(self, schema, attributes, cancellable):
        self.calls += 1
        if self.error:
            raise self.error
        return self.value


def keyring(secret):
    store = KeyringSecretStore.__new__(KeyringSecretStore)
    store._lock = threading.Lock()
    store._cache = {}
    store._down_until = 0.0
    store._secret = secret
    store._schema = None
    store.available = True
    return store


def test_an_unresponsive_keyring_is_asked_only_once(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(secretstore.time, "monotonic", lambda: clock[0])
    secret = FakeSecret(FakeGLibError("g-io-error-quark", "Timeout was reached"))
    store = keyring(secret)
    assert store.lookup("deepseek") is None
    assert store.lookup("poe") is None
    assert secret.calls == 1  # the second lookup did not wait another 25 s
    secret.error = None
    clock[0] += secretstore.OUTAGE_PAUSE + 1
    assert store.lookup("poe") == "sk-test"


def test_other_keyring_errors_do_not_pause_lookups():
    secret = FakeSecret(FakeGLibError("secret-error-quark", "Item is locked"))
    store = keyring(secret)
    store.lookup("deepseek")
    store.lookup("poe")
    assert secret.calls == 2
    assert not is_outage(secret.error)
    assert is_outage(FakeGLibError("g-dbus-error-quark", "ServiceUnknown"))


def test_lookups_are_cached():
    secret = FakeSecret()
    store = keyring(secret)
    assert store.lookup("poe") == store.lookup("poe") == "sk-test"
    assert secret.calls == 1
