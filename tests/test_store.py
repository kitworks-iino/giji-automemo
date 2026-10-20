import keyring
import pytest

from giji.store import Secrets, Store


def test_record_survives_reopen(tmp_path):
    store = Store(tmp_path)
    store.put_session({"id": "one", "started_at": "2026-01-01", "status": "recording"})
    store.patch_session("one", {"transcription_complete": True, "segments": [{"text": "記録"}]})
    store.close()
    store = Store(tmp_path)
    assert store.session("one")["transcription_complete"] is True
    assert store.session("one")["segments"] == [{"text": "記録"}]
    store.close()


def test_secret_cache_chunking_and_cleanup(monkeypatch):
    data = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, key: data.get(key))
    monkeypatch.setattr(keyring, "set_password", lambda service, key, val: data.__setitem__(key, val))
    monkeypatch.setattr(keyring, "delete_password", lambda service, key: data.pop(key))
    secrets = Secrets()
    cache = "あ" * 4000
    secrets.set("cache", cache)
    assert secrets.get("cache") == cache
    assert max(len(value.encode("utf-16-le")) for value in data.values()) <= 2560
    secrets.set("cache", "new")
    assert secrets.get("cache") == "new"
    assert len(data) == 2
    secrets.delete("cache")
    assert not data


def test_failed_secret_update_preserves_previous_generation(monkeypatch):
    data = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, key: data.get(key))
    monkeypatch.setattr(keyring, "set_password", lambda service, key, val: data.__setitem__(key, val))
    monkeypatch.setattr(keyring, "delete_password", lambda service, key: data.pop(key))
    secrets = Secrets()
    secrets.set("cache", "old")
    def broken(service, key, value):
        if value == "broken":
            raise keyring.errors.KeyringError("unavailable")
        data[key] = value
    monkeypatch.setattr(keyring, "set_password", broken)
    with pytest.raises(RuntimeError):
        secrets.set("cache", "broken")
    assert secrets.get("cache") == "old"


def test_public_settings_never_store_api_key(tmp_path):
    store = Store(tmp_path)
    store.update_settings({"api_key": "should-not-be-saved", "recording_consent": True})
    assert "api_key" not in store.settings()
    store.close()
