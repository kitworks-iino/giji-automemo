import json
import os
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import giji.teams_connection as connection
from giji.teams_connection import (
    JOURNAL_NAME,
    Listener,
    ProcessIdentity,
    TeamsConnectionError,
    TeamsConnectionManager,
    validate_cdp_url,
    validate_listener_ownership,
)


PORT = 49231
ENDPOINT = f"http://127.0.0.1:{PORT}"
FLAGS = f"--remote-debugging-port={PORT} --remote-debugging-address=127.0.0.1"
PACKAGE = "MSTeams_26000.100.200.300_x64__8wekyb3d8bbwe"
TEAMS = ProcessIdentity(10, "ms-teams.exe", rf"C:\Program Files\WindowsApps\{PACKAGE}\ms-teams.exe",
                        "S-1-user", 1, PACKAGE)
WEBVIEW = ProcessIdentity(11, "msedgewebview2.exe", r"C:\Program Files\EdgeWebView\msedgewebview2.exe",
                          "S-1-user", 10)


class Backend:
    supported = True

    def __init__(self):
        self.value = None
        self.writes = []
        self.checks = 0
        self.verified = []
        self.alive = False
        self.conflict = False
        self.failure = None
        self.private_paths = set()

    def owner(self):
        return "S-1-user"

    def check_policy(self):
        self.checks += 1
        if self.conflict:
            raise TeamsConnectionError("組織の設定があるため変更できません。")

    def read_value(self):
        return self.value

    def write_value(self, value):
        self.value = (1, value)
        self.writes.append(value)

    def delete_if_equal(self, value):
        if self.value == (1, value):
            self.value = None
            self.writes.append(None)

    def secure_file(self, path):
        os.chmod(path, 0o600)
        self.private_paths.add(str(path))

    def file_is_private(self, path):
        return str(path) in self.private_paths and (
            sys.platform == "win32" or path.stat().st_mode & 0o777 == 0o600
        )

    def parent_identity(self):
        return 100, 123.0

    def parent_alive(self, pid, created):
        return self.alive

    def verify_listener(self, port):
        self.verified.append(port)
        if self.failure:
            raise self.failure


def manager(tmp_path, backend=None, **kwargs):
    return TeamsConnectionManager(
        tmp_path, backend=backend or Backend(), clock=kwargs.pop("clock", lambda: 1000),
        port_factory=lambda: PORT, helper_launcher=kwargs.pop("helper_launcher", lambda *_: None), **kwargs,
    )


def test_construction_and_status_do_not_enable_debugging_or_probe_processes(tmp_path):
    backend = Backend()
    value = manager(tmp_path, backend)
    assert value.status()["state"] == "needs_setup"
    value.status()["connected"] = True
    assert value.status()["connected"] is False
    assert backend.checks == 0
    assert backend.writes == backend.verified == []
    assert list(tmp_path.iterdir()) == []


def test_prepare_journals_before_write_then_connection_restores_own_value(tmp_path):
    backend = Backend()
    launched = []

    def launch(path, journal):
        assert backend.value is None
        assert json.loads(path.read_text()) == journal
        assert backend.file_is_private(path)
        launched.append(journal)

    value = manager(tmp_path, backend, helper_launcher=launch)
    assert value.prepare()["state"] == "waiting_for_restart"
    assert backend.value == (1, FLAGS)
    assert len(launched) == 1
    assert launched[0]["expires_at"] == 1600
    assert value.connect_endpoint() == ENDPOINT
    assert backend.verified == [PORT]
    assert backend.value is None
    assert not (tmp_path / JOURNAL_NAME).exists()
    assert value.status()["connected"] is True
    assert value.status()["policy_restored"] is True
    assert value.status()["debug_active"] is True
    # Every use revalidates ownership; a cached URL is never blindly returned.
    assert value.connect_endpoint() == ENDPOINT
    assert backend.verified == [PORT, PORT]


@pytest.mark.parametrize("source", ["policy", "registry", "helper", "late_change"])
def test_conflicts_and_failed_cleanup_helper_never_override_settings(tmp_path, source):
    backend = Backend()
    if source == "policy":
        backend.conflict = True
    if source == "registry":
        backend.value = (1, "--existing-setting")

    def launch(*_):
        if source == "helper":
            raise OSError("private local path and error must not be disclosed")
        if source == "late_change":
            backend.value = (1, "--administrator-change")

    value = manager(tmp_path, backend, helper_launcher=launch)
    with pytest.raises(TeamsConnectionError) as caught:
        value.prepare()
    assert "private local path" not in str(caught.value)
    assert backend.writes == []
    if source == "registry":
        assert backend.value == (1, "--existing-setting")
    if source == "late_change":
        assert backend.value == (1, "--administrator-change")
    assert not (tmp_path / JOURNAL_NAME).exists()


def test_failed_private_file_setup_leaves_no_setting_or_broken_journal(tmp_path):
    backend = Backend()
    backend.secure_file = Mock(side_effect=PermissionError("private error"))
    value = manager(tmp_path, backend)
    with pytest.raises(TeamsConnectionError):
        value.prepare()
    assert backend.writes == []
    assert not (tmp_path / JOURNAL_NAME).exists()


def test_disconnect_restores_without_stopping_teams_and_can_prepare_again(tmp_path):
    backend = Backend()
    value = manager(tmp_path, backend)
    value.prepare()
    value.connect_endpoint()
    result = value.disconnect()
    assert result["state"] == "disconnected"
    assert result["needs_restart"] is True
    assert result["debug_active"] is True  # registry removal cannot close live CDP
    with pytest.raises(TeamsConnectionError):
        value.connect_endpoint()
    assert value.prepare()["state"] == "waiting_for_restart"
    value.close()
    assert backend.value is None
    with pytest.raises(TeamsConnectionError):
        value.prepare()


def test_restoration_never_deletes_an_external_change(tmp_path):
    backend = Backend()
    value = manager(tmp_path, backend)
    value.prepare()
    backend.value = (1, "--some-new-policy")
    value.close()
    assert backend.value == (1, "--some-new-policy")
    assert not (tmp_path / JOURNAL_NAME).exists()


def test_constructor_recovers_own_crash_journal_only(tmp_path):
    backend = Backend()
    manager(tmp_path, backend).prepare()
    recovered = manager(tmp_path, backend)
    assert backend.value is None
    assert "復元" in recovered.status()["detail"]
    assert recovered.status()["needs_restart"] is True
    assert not (tmp_path / JOURNAL_NAME).exists()


def test_constructor_does_not_recover_a_live_other_instance(tmp_path):
    backend = Backend()
    manager(tmp_path, backend).prepare()
    backend.alive = True
    recovered = manager(tmp_path, backend)
    assert recovered.status()["state"] == "blocked"
    assert backend.value == (1, FLAGS)


@pytest.mark.parametrize("fault", ["foreign_owner", "invalid_port", "public_file", "symlink", "invalid_json"])
def test_untrusted_journals_do_not_authorize_registry_deletion(tmp_path, fault):
    backend = Backend()
    manager(tmp_path, backend).prepare()
    path = tmp_path / JOURNAL_NAME
    data = json.loads(path.read_text())
    if fault == "foreign_owner":
        data["owner"] = "another-user"
        path.write_text(json.dumps(data))
    elif fault == "invalid_port":
        data["port"] = "49231 --arbitrary-argument"
        path.write_text(json.dumps(data))
    elif fault == "public_file":
        path.chmod(0o644)
        backend.private_paths.discard(str(path))
    elif fault == "invalid_json":
        path.write_text("not a journal")
    else:
        if sys.platform == "win32":
            pytest.skip("Windows symlink creation requires additional developer privileges")
        moved = tmp_path / "other.json"
        path.rename(moved)
        path.symlink_to(moved)
    recovered = manager(tmp_path, backend)
    assert recovered.status()["state"] == "blocked"
    assert backend.value == (1, FLAGS)


def test_expired_preparation_restores_settings_and_requires_prepare(tmp_path):
    backend, now = Backend(), [1000]
    value = manager(tmp_path, backend, clock=lambda: now[0])
    value.prepare()
    backend.failure = TeamsConnectionError("Teams 接続待ち")
    now[0] = 1601
    with pytest.raises(TeamsConnectionError, match="有効時間"):
        value.connect_endpoint()
    assert backend.value is None
    assert value.status()["state"] == "needs_setup"
    assert backend.verified == []


def test_disconnect_before_connect_still_warns_about_possible_live_listener(tmp_path):
    value = manager(tmp_path)
    value.prepare()
    assert value.disconnect()["needs_restart"] is True


def test_failed_listener_verification_never_returns_endpoint(tmp_path):
    backend = Backend()
    value = manager(tmp_path, backend)
    value.prepare()
    backend.failure = OSError("secret process path")
    with pytest.raises(TeamsConnectionError) as caught:
        value.connect_endpoint()
    assert "secret" not in str(caught.value)
    assert not value.status()["connected"]


def test_lost_established_listener_requires_new_preparation(tmp_path):
    backend = Backend()
    value = manager(tmp_path, backend)
    value.prepare()
    value.connect_endpoint()
    assert backend.value is None
    backend.failure = TeamsConnectionError("接続待ち")
    with pytest.raises(TeamsConnectionError, match="準備し直して"):
        value.connect_endpoint()
    assert value.status()["state"] == "needs_setup"
    assert not value.status()["connected"]
    calls = len(backend.verified)
    with pytest.raises(TeamsConnectionError, match="まず"):
        value.connect_endpoint()
    assert len(backend.verified) == calls
    assert value.prepare()["state"] == "waiting_for_restart"
    assert backend.value == (1, FLAGS)


def test_failed_registry_write_and_failed_restore_do_not_claim_restored(tmp_path):
    backend = Backend()

    def partial_write(value):
        backend.value = (1, value)
        raise OSError("write failed after committing")

    backend.write_value = partial_write
    backend.delete_if_equal = Mock(side_effect=PermissionError("restore denied"))
    value = manager(tmp_path, backend)
    with pytest.raises(TeamsConnectionError):
        value.prepare()
    assert not value.status()["policy_restored"]
    assert (tmp_path / JOURNAL_NAME).exists()


def test_cleanup_helper_restores_after_parent_exit_without_starting_app(tmp_path, monkeypatch):
    backend = Backend()
    manager(tmp_path, backend).prepare()
    path = tmp_path / JOURNAL_NAME
    journal = json.loads(path.read_text())
    monkeypatch.setattr(connection, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(connection, "_WindowsBackend", lambda: backend)
    assert connection.cleanup_main([str(path), "100", "123.0", journal["nonce"]]) == 0
    assert backend.value is None
    assert not path.exists()


def test_cleanup_helper_restores_after_ttl_even_if_parent_lives(tmp_path, monkeypatch):
    backend = Backend()
    manager(tmp_path, backend).prepare()
    backend.alive = True
    path = tmp_path / JOURNAL_NAME
    journal = json.loads(path.read_text())
    monkeypatch.setattr(connection, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(connection, "_WindowsBackend", lambda: backend)
    monkeypatch.setattr(connection, "time", SimpleNamespace(time=lambda: 1601, monotonic=lambda: 100))
    assert connection.cleanup_main([str(path), "100", "123.0", journal["nonce"]]) == 0
    assert backend.value is None


def test_old_cleanup_generation_cannot_touch_new_setup(tmp_path):
    backend = Backend()
    value = manager(tmp_path, backend)
    value.prepare()
    old_nonce = json.loads((tmp_path / JOURNAL_NAME).read_text())["nonce"]
    value.disconnect()
    value.prepare()
    assert connection._restore_journal(tmp_path / JOURNAL_NAME, backend, old_nonce) is False
    assert backend.value == (1, FLAGS)
    assert (tmp_path / JOURNAL_NAME).exists()


@pytest.mark.parametrize("mode", ["ready", "wrong_nonce", "public_receipt", "exited", "silent"])
def test_helper_launch_requires_private_live_readiness_receipt(tmp_path, monkeypatch, mode):
    backend = Backend()
    value = manager(tmp_path, backend)
    value.prepare()
    path = tmp_path / JOURNAL_NAME
    journal = json.loads(path.read_text())
    ready = connection._ready_path(path, journal["nonce"])
    if mode in {"ready", "wrong_nonce", "public_receipt"}:
        connection._write_ready(ready, backend, journal["nonce"] if mode != "wrong_nonce" else "a" * 32)
        if mode == "public_receipt":
            ready.chmod(0o644)
            backend.private_paths.discard(str(ready))
    process = Mock()
    process.poll.return_value = 1 if mode == "exited" else None
    monkeypatch.setattr(connection.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(connection, "_WindowsBackend", lambda: backend)
    # Deterministic deadline: no actual wait or subprocess is involved.
    ticks = iter([0, 0, 6])
    monkeypatch.setattr(connection, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None))
    if mode == "ready":
        connection._launch_cleanup(path, journal)
        assert not ready.exists()
        process.terminate.assert_not_called()
    else:
        with pytest.raises(TeamsConnectionError, match="自動復元"):
            connection._launch_cleanup(path, journal)
        if mode != "exited":
            process.terminate.assert_called_once()


def test_helper_acknowledges_valid_journal_before_monitoring(tmp_path, monkeypatch):
    backend = Backend()
    manager(tmp_path, backend).prepare()
    backend.alive = True
    path = tmp_path / JOURNAL_NAME
    journal = json.loads(path.read_text())
    ready = connection._ready_path(path, journal["nonce"])
    monkeypatch.setattr(connection, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(connection, "_WindowsBackend", lambda: backend)

    def sleep(_):
        assert connection._ready_matches(ready, backend, journal["nonce"])
        backend.alive = False

    monkeypatch.setattr(connection, "time", SimpleNamespace(time=lambda: 1001, monotonic=lambda: 100, sleep=sleep))
    assert connection.cleanup_main([str(path), "100", "123.0", journal["nonce"]]) == 0
    assert backend.value is None
    assert not ready.exists()


@pytest.mark.parametrize("path", ["/json/list", "/json/version"])
def test_discovery_url_allowlist(path):
    assert validate_cdp_url(ENDPOINT + path, ENDPOINT)


@pytest.mark.parametrize("kind", ["browser", "page"])
def test_devtools_socket_allowlist(kind):
    assert validate_cdp_url(f"ws://127.0.0.1:{PORT}/devtools/{kind}/abc-123", ENDPOINT, True)


@pytest.mark.parametrize("url", [
    f"http://localhost:{PORT}/json/list", f"http://127.1:{PORT}/json/list",
    f"http://127.0.0.1:{PORT + 1}/json/list", "https://example.com/json/list",
    ENDPOINT + "/json/new", ENDPOINT + "/json/close/abc", ENDPOINT + "/json/list?secret=x",
    ENDPOINT + "/json/list#x", ENDPOINT + "/json/%6cist", ENDPOINT + "/json/../json/list",
    f"http://user@127.0.0.1:{PORT}/json/list", ENDPOINT + "\\@evil.example/json/list",
    ENDPOINT + "/json/list\n", "\t" + ENDPOINT + "/json/list",
    f"http://[::1]:{PORT}/json/list", f"http://127.0.0.1:{PORT:06d}/json/list",
])
def test_discovery_url_rejects_origin_and_path_bypasses(url):
    assert not validate_cdp_url(url, ENDPOINT)


@pytest.mark.parametrize("url", [
    f"ws://127.0.0.1:{PORT}/devtools/browser/abc?token=x",
    f"ws://127.0.0.1:{PORT}/devtools/browser/../page/abc",
    f"ws://127.0.0.1:{PORT}/devtools/browser/a%2fb",
    f"ws://127.0.0.1:{PORT}/devtools/browser/", f"ws://localhost:{PORT}/devtools/page/abc",
    f"ws://127.0.0.1:{PORT}/devtools/other/abc", f"wss://127.0.0.1:{PORT}/devtools/page/abc",
])
def test_websocket_urls_reject_bypasses(url):
    assert not validate_cdp_url(url, ENDPOINT, True)


def test_listener_requires_windows_packaged_teams_ancestor():
    validate_listener_ownership([Listener("127.0.0.1", PORT, 11)], {10: TEAMS, 11: WEBVIEW}, PORT, "S-1-user")
    validate_listener_ownership([Listener("127.0.0.1", PORT, 10)], {10: TEAMS}, PORT, "S-1-user")


@pytest.mark.parametrize("change", [
    "foreign_listener", "foreign_ancestor", "fake_package", "renamed_executable", "browser",
    "missing_parent", "cycle", "unknown_pid", "wildcard", "wildcard_ipv6", "public_ip", "missing_ipv4",
])
def test_listener_rejects_unowned_or_non_loopback_endpoint(change):
    listeners = [Listener("127.0.0.1", PORT, 11)]
    processes = {10: TEAMS, 11: WEBVIEW}
    if change == "foreign_listener":
        processes[11] = replace(WEBVIEW, owner="other")
    elif change == "foreign_ancestor":
        processes[10] = replace(TEAMS, owner="other")
    elif change == "fake_package":
        processes[10] = replace(TEAMS, package="")
    elif change == "renamed_executable":
        processes[10] = replace(TEAMS, executable=r"C:\fake\ms-teams.exe")
    elif change == "browser":
        processes[11] = replace(WEBVIEW, name="msedge.exe", executable=r"C:\Edge\msedge.exe")
    elif change == "missing_parent":
        processes.pop(10)
    elif change == "cycle":
        processes[11] = replace(WEBVIEW, parent_pid=11)
    elif change == "unknown_pid":
        listeners = [Listener("127.0.0.1", PORT, None)]
    elif change == "missing_ipv4":
        listeners = [Listener("::1", PORT, 11)]
    else:
        address = {"wildcard": "0.0.0.0", "wildcard_ipv6": "::", "public_ip": "192.0.2.1"}[change]
        listeners.append(Listener(address, PORT, 11))
    with pytest.raises(TeamsConnectionError):
        validate_listener_ownership(listeners, processes, PORT, "S-1-user")


@pytest.mark.parametrize("hive,view,name", [(1, 32, "*"), (2, 64, "ms-teams.exe"), (1, 64, "MSTeams")])
def test_native_policy_check_blocks_machine_and_user_conflicts(monkeypatch, hive, view, name):
    import sys

    winreg = SimpleNamespace(HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2, KEY_WOW64_32KEY=32, KEY_WOW64_64KEY=64)
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    backend = connection._WindowsBackend()
    backend._values = lambda h, p, v: {name.lower(): (1, "--managed")} if (
        h == hive and v == view and p == connection.REGISTRY_KEY
    ) else {}
    with pytest.raises(TeamsConnectionError):
        backend.check_policy()


def test_native_policy_check_blocks_disabled_devtools_and_environment(monkeypatch):
    import sys

    winreg = SimpleNamespace(HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2, KEY_WOW64_32KEY=32, KEY_WOW64_64KEY=64)
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    backend = connection._WindowsBackend()
    backend._values = lambda h, p, v: {"developertoolsavailability": (4, 2)} if p.endswith("Edge") else {}
    with pytest.raises(TeamsConnectionError):
        backend.check_policy()
    backend._values = lambda *_: {}
    monkeypatch.setenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--some-existing-argument")
    with pytest.raises(TeamsConnectionError):
        backend.check_policy()


def test_other_app_specific_webview_settings_are_not_changed_or_blocked(monkeypatch):
    import sys

    winreg = SimpleNamespace(HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2, KEY_WOW64_32KEY=32, KEY_WOW64_64KEY=64)
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    backend = connection._WindowsBackend()
    backend._values = lambda h, p, v: {"other.exe": (1, "--existing")} if p == connection.REGISTRY_KEY else {}
    backend.check_policy()
