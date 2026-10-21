"""Explicit, temporary CDP setup for the signed-in Windows Teams desktop app.

Only the per-executable WebView2 argument is changed. Existing policy wins. A
private write-ahead journal, an independent cleanup process, and startup recovery
remove our exact value; no Teams process is ever stopped or started here.

Removing the argument cannot close a running WebView2 debugging listener. The
user must completely quit/reopen Teams to close that session after disconnecting.
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import ntpath
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit


REGISTRY_KEY = r"Software\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments"
REGISTRY_VALUE = "ms-teams.exe"
JOURNAL_NAME = "teams-connection-journal.json"
PREPARATION_TTL_SECONDS = 600
_PACKAGE = re.compile(r"MSTeams_[0-9.]+_(?:x64|x86|arm64|neutral)__8wekyb3d8bbwe", re.I)


class TeamsConnectionError(RuntimeError):
    """A safe public error: never include raw registry/process/CDP output."""


def validate_cdp_url(url: str, endpoint: str, websocket: bool = False) -> bool:
    """Allow only fixed discovery paths or DevTools sockets at our exact port.

    No hostname resolution, redirects, credentials, query, fragment, encoded
    paths, alternate numeric IP spellings, or arbitrary HTTP/CDP commands.
    """
    try:
        if not isinstance(url, str) or not isinstance(endpoint, str):
            return False
        if any(ord(char) <= 32 or ord(char) >= 127 for char in url + endpoint):
            return False
        base, candidate = urlsplit(endpoint), urlsplit(url)
        if (
            base.scheme != "http" or base.hostname != "127.0.0.1"
            or not base.port or not 1024 <= base.port <= 65535
            or base.username is not None or base.password is not None
            or base.path not in ("", "/") or base.query or base.fragment
            or base.netloc != f"127.0.0.1:{base.port}"
        ):
            return False
        if (
            candidate.scheme != ("ws" if websocket else "http")
            or candidate.netloc != base.netloc
            or candidate.username is not None or candidate.password is not None
            or candidate.query or candidate.fragment
        ):
            return False
        if websocket:
            return re.fullmatch(r"/devtools/(?:browser|page)/[A-Za-z0-9_-]{1,160}", candidate.path) is not None
        return candidate.path in {"/json/list", "/json/version"}
    except (ValueError, TypeError):
        return False


def _arguments(port: int) -> str:
    return f"--remote-debugging-port={port} --remote-debugging-address=127.0.0.1"


@dataclass(frozen=True)
class Listener:
    address: str
    port: int
    pid: int | None


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    name: str
    executable: str
    owner: str
    parent_pid: int
    package: str = ""


def validate_listener_ownership(
    listeners: list[Listener], processes: Mapping[int, ProcessIdentity], port: int, owner: str,
) -> None:
    """Fail closed before any HTTP request, even for a plausible CDP server.

    Windows package identity proves the Teams ancestor belongs to Microsoft's
    MSTeams package; an arbitrary executable named ms-teams.exe is insufficient.
    """
    relevant = [entry for entry in listeners if entry.port == port]
    if not relevant or not any(item.address == "127.0.0.1" for item in relevant):
        raise TeamsConnectionError(
            "Teams の接続待ちです。会議中でないことを確認し、Teams を完全に終了して開き直してください。"
        )
    for listener in relevant:
        try:
            if not ipaddress.ip_address(listener.address).is_loopback:
                raise ValueError("non-loopback")
        except ValueError:
            raise TeamsConnectionError("接続先がローカル限定ではないため、Teams に接続できません。") from None
        process = processes.get(listener.pid)
        if process is None or process.name.lower() not in {"ms-teams.exe", "msedgewebview2.exe"}:
            raise TeamsConnectionError("接続先が Teams のプロセスだと確認できません。接続を停止しました。")
        seen: set[int] = set()
        for _ in range(24):
            if (
                process is None or process.pid in seen or process.owner != owner
                or ntpath.basename(process.executable).lower() != process.name.lower()
            ):
                break
            seen.add(process.pid)
            if process.name.lower() == "ms-teams.exe":
                if (
                    _PACKAGE.fullmatch(process.package)
                    and ntpath.basename(ntpath.dirname(process.executable)).lower() == process.package.lower()
                ):
                    break
                raise TeamsConnectionError("Microsoft Teams のアプリ識別情報を確認できません。")
            if process.name.lower() != "msedgewebview2.exe":
                break
            process = processes.get(process.parent_pid)
        else:
            process = None
        if process is None or process.name.lower() != "ms-teams.exe" or process.pid not in seen:
            raise TeamsConnectionError("この Windows ユーザーの Teams 接続だと確認できません。")


class _WindowsBackend:
    """Native operations kept behind a small seam for non-Windows tests."""

    supported = sys.platform == "win32"

    def owner(self) -> str:
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, 1)[0])
        finally:
            token.Close()

    def _values(self, hive, path: str, view: int) -> dict:
        import winreg

        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as key:
                values = {}
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, value, kind = winreg.EnumValue(key, index)
                    values[name.lower()] = (kind, value)
                return values
        except FileNotFoundError:
            return {}

    def check_policy(self) -> None:
        import winreg

        if any(os.environ.get(name) for name in (
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER",
            "WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER",
        )):
            raise TeamsConnectionError("WebView2 の既存環境設定があるため変更できません。管理者に確認してください。")
        edge = r"Software\Policies\Microsoft\Edge"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                values = self._values(hive, REGISTRY_KEY, view)
                # Any Teams-specific, path-specific, default, or wildcard value
                # can control this host. Values explicitly for other apps are fine.
                if any(
                    not name or "*" in name or "teams" in name or "\\" in name or "/" in name
                    for name in values
                ):
                    raise TeamsConnectionError(
                        "Teams / WebView2 に既存の組織設定または接続設定があります。上書きせず停止しました。"
                    )
                for path in (edge, edge + r"\WebView2"):
                    policy = self._values(hive, path, view)
                    if (
                        "additionalbrowserarguments" in policy
                        or policy.get("developertoolsavailability", (None, None))[1] == 2
                        or policy.get("remotedebuggingallowed", (None, None))[1] == 0
                    ):
                        raise TeamsConnectionError("組織の WebView2 / 開発者ツール設定により接続を準備できません。")
                for policy_name in ("DeveloperToolsAvailability", "RemoteDebuggingAllowed"):
                    policy = self._values(hive, edge + "\\WebView2\\" + policy_name, view)
                    if any(not name or "*" in name or "teams" in name for name in policy):
                        raise TeamsConnectionError("組織の WebView2 設定を変更せず、接続を停止しました。")

    def read_value(self):
        import winreg

        return self._values(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, winreg.KEY_WOW64_64KEY).get(REGISTRY_VALUE)

    def write_value(self, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
        ) as key:
            winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, value)

    def delete_if_equal(self, value: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0,
                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
            ) as key:
                current, kind = winreg.QueryValueEx(key, REGISTRY_VALUE)
                if kind == winreg.REG_SZ and current == value:
                    winreg.DeleteValue(key, REGISTRY_VALUE)
        except FileNotFoundError:
            pass
        # Leave empty container keys: deleting them can destroy another app's
        # concurrently added settings. Only our single value affects behavior.

    def secure_file(self, path: Path) -> None:
        import win32security

        sid = win32security.ConvertStringSidToSid(self.owner())
        acl = win32security.ACL()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, 0x1F01FF, sid)
        win32security.SetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None,
        )

    def file_is_private(self, path: Path) -> bool:
        import win32security

        security = win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
        if win32security.ConvertSidToStringSid(security.GetSecurityDescriptorOwner()) != self.owner():
            return False
        acl = security.GetSecurityDescriptorDacl()
        if acl is None or not acl.GetAceCount():
            return False
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
                return False
            if win32security.ConvertSidToStringSid(ace[2]) != self.owner():
                return False
        return True

    def parent_identity(self) -> tuple[int, float]:
        import psutil

        return os.getpid(), psutil.Process().create_time()

    def parent_alive(self, pid: int, created: float) -> bool:
        import psutil

        try:
            return abs(psutil.Process(pid).create_time() - created) < 0.001
        except (psutil.Error, OSError):
            return False

    def _process(self, pid: int) -> ProcessIdentity:
        import psutil
        import win32api
        import win32con
        import win32security
        from ctypes import wintypes

        process = psutil.Process(pid)
        with process.oneshot():
            name, executable, parent = process.name(), process.exe(), process.ppid()
        handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        token = None
        try:
            token = win32security.OpenProcessToken(handle, win32con.TOKEN_QUERY)
            owner = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, 1)[0])
            package = ""
            if name.lower() == "ms-teams.exe":
                query = ctypes.WinDLL("kernel32", use_last_error=True).GetPackageFullName
                query.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
                query.restype = wintypes.LONG
                length = wintypes.UINT(0)
                if query(int(handle), ctypes.byref(length), None) == 122 and 0 < length.value <= 512:
                    buffer = ctypes.create_unicode_buffer(length.value)
                    if query(int(handle), ctypes.byref(length), buffer) == 0:
                        package = buffer.value
            return ProcessIdentity(pid, name, executable, owner, parent, package)
        finally:
            if token is not None:
                token.Close()
            handle.Close()

    def verify_listener(self, port: int) -> None:
        import psutil

        try:
            listeners = [
                Listener(item.laddr.ip, item.laddr.port, item.pid)
                for item in psutil.net_connections(kind="tcp")
                if item.status == psutil.CONN_LISTEN and item.laddr and item.laddr.port == port
            ]
            processes = {}
            for entry in listeners:
                pid = entry.pid
                for _ in range(24):
                    if not pid or pid in processes:
                        break
                    process = self._process(pid)
                    processes[pid] = process
                    if process.name.lower() != "msedgewebview2.exe":
                        break
                    pid = process.parent_pid
            validate_listener_ownership(listeners, processes, port, self.owner())
        except TeamsConnectionError:
            raise
        except Exception:
            raise TeamsConnectionError("Teams の接続元を安全に確認できません。Teams を開き直して再試行してください。") from None


def _choose_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _ready_path(path: Path, nonce: str) -> Path:
    return path.with_name(f"teams-connection-ready-{nonce}.json")


def _write_ready(path: Path, backend, nonce: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        backend.secure_file(path)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            fd = -1
            handle.write(nonce)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _ready_matches(path: Path, backend, nonce: str) -> bool:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != 32 or not backend.file_is_private(path):
            return False
        return path.read_text(encoding="ascii") == nonce
    except Exception:
        return False


def _launch_cleanup(path: Path, journal: dict) -> None:
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command += ["-m", "giji.teams_connection"]
    command += [
        "--teams-connection-cleanup", str(path), str(journal["pid"]),
        str(journal["process_created"]), journal["nonce"],
    ]
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, creationflags=flags,
    )
    # Popen succeeding does not prove a frozen helper can import its native
    # dependencies. Require its private, generation-bound readiness receipt.
    ready = _ready_path(path, journal["nonce"])
    backend = _WindowsBackend()
    confirmed = False
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if _ready_matches(ready, backend, journal["nonce"]):
                if process.poll() is None:
                    confirmed = True
                    return
                break
            time.sleep(0.05)
        raise TeamsConnectionError("一時設定を自動復元する処理を起動できません。設定は変更していません。")
    finally:
        if _ready_matches(ready, backend, journal["nonce"]):
            ready.unlink(missing_ok=True)
        if not confirmed and process.poll() is None:
            # Only our just-created helper is stopped, never Teams or WebView2.
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()


def _read_journal(path: Path, backend) -> dict | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or not backend.file_is_private(path):
        raise TeamsConnectionError("Teams 接続の復元ファイルを安全に確認できません。接続準備を停止しました。")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.loads(handle.read(4097))
        if (
            not isinstance(value, dict) or value.get("version") != 1
            or value.get("owner") != backend.owner()
            or type(value.get("port")) is not int or not 1024 <= value["port"] <= 65535
            or not re.fullmatch(r"[a-f0-9]{32}", value.get("nonce", ""))
            or type(value.get("pid")) is not int or value["pid"] <= 0
            or type(value.get("process_created")) not in (int, float)
            or type(value.get("expires_at")) not in (int, float)
        ):
            raise ValueError("invalid journal")
        return value
    except (ValueError, TypeError, OSError):
        raise TeamsConnectionError("Teams 接続の復元ファイルが不正です。既存の設定は変更していません。") from None


def _restore_journal(path: Path, backend, nonce: str | None = None) -> bool:
    journal = _read_journal(path, backend)
    if journal is None:
        return True
    if nonce is not None and journal["nonce"] != nonce:
        return False
    backend.delete_if_equal(_arguments(journal["port"]))
    # Remove only this generation; a stale cleanup helper cannot erase a new one.
    current = _read_journal(path, backend)
    if current and current["nonce"] == journal["nonce"]:
        path.unlink(missing_ok=True)
    return True


class TeamsConnectionManager:
    def __init__(
        self, data_dir: Path, *, backend=None, clock: Callable[[], float] = time.time,
        port_factory: Callable[[], int] = _choose_port, helper_launcher: Callable = _launch_cleanup,
    ):
        self._backend = backend or _WindowsBackend()
        self._clock, self._port_factory, self._helper_launcher = clock, port_factory, helper_launcher
        self._path = Path(data_dir).resolve() / JOURNAL_NAME
        self._lock = threading.RLock()
        self._port: int | None = None
        self._nonce: str | None = None
        self._closed = False
        self._public = {
            "provider": "teams_desktop", "state": "needs_setup", "connected": False,
            "detail": "Teams デスクトップ接続を準備してください。Microsoft の追加登録は不要です。",
            "needs_restart": False, "policy_restored": True, "debug_active": False, "expires_at": None,
        }
        if not self._backend.supported:
            self._set("unavailable", "Teams デスクトップ接続は Windows 11 に対応しています。")
        else:
            try:
                journal = _read_journal(self._path, self._backend)
                if journal:
                    self._public["policy_restored"] = False
                    if self._backend.parent_alive(journal["pid"], journal["process_created"]):
                        raise TeamsConnectionError("別の giji が Teams 接続を準備しています。")
                    _restore_journal(self._path, self._backend, journal["nonce"])
                    self._public.update(
                        detail="前回の一時接続設定を復元しました。起動中の接続口が残る可能性があるため、"
                        "会議後に Teams を完全に終了して開き直してください。",
                        needs_restart=True, policy_restored=True,
                    )
            except Exception as exc:
                self._set("blocked", self._safe_error(exc))

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return str(exc) if isinstance(exc, TeamsConnectionError) else "Teams の一時接続設定を処理できません。"

    def _set(self, state: str, detail: str, **changes) -> dict:
        self._public.update(state=state, detail=detail, **changes)
        return dict(self._public)

    def status(self) -> dict:
        """Never probes Teams, reads policy, opens sockets or writes anything."""
        with self._lock:
            return dict(self._public)

    def prepare(self) -> dict:
        """Explicit UI action; caller must prevent setup while in a meeting."""
        with self._lock:
            if not self._backend.supported or self._closed:
                raise TeamsConnectionError("この環境では Teams 接続を準備できません。")
            if self._public["connected"]:
                return self.status()
            if self._nonce:
                self.restore()
            self._port = None
            journal_complete = False
            try:
                if self._path.exists():
                    raise TeamsConnectionError("前回の Teams 接続設定の復元が必要です。giji を開き直してください。")
                self._backend.check_policy()
                if self._backend.read_value() is not None:
                    raise TeamsConnectionError("Teams の既存接続設定は上書きできません。")
                port = self._port_factory()
                if type(port) is not int or not 1024 <= port <= 65535:
                    raise TeamsConnectionError("ローカル接続ポートを準備できません。")
                pid, created = self._backend.parent_identity()
                journal = {
                    "version": 1, "owner": self._backend.owner(), "nonce": secrets.token_hex(16),
                    "port": port, "pid": pid, "process_created": created,
                    "expires_at": self._clock() + PREPARATION_TTL_SECONDS,
                }
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self._nonce = journal["nonce"]
                try:
                    self._backend.secure_file(self._path)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        fd = -1
                        json.dump(journal, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    journal_complete = True
                finally:
                    if fd >= 0:
                        os.close(fd)
                self._helper_launcher(self._path, journal)
                # The journal and cleanup process must exist before the write.
                self._backend.check_policy()
                if self._backend.read_value() is not None:
                    raise TeamsConnectionError("準備中に Teams の設定が変更されたため、上書きせず停止しました。")
                pending = _read_journal(self._path, self._backend)
                if not pending or pending["nonce"] != self._nonce or self._clock() >= pending["expires_at"] - 5:
                    raise TeamsConnectionError("接続準備の有効時間が過ぎました。もう一度準備してください。")
                self._public["policy_restored"] = False
                self._backend.write_value(_arguments(port))
                self._port = port
                return self._set(
                    "waiting_for_restart",
                    "10 分以内に、会議中でないことを確認して Teams を完全に終了し、開き直してください。"
                    "giji は Teams を自動終了しません。",
                    connected=False, needs_restart=True, policy_restored=False, expires_at=journal["expires_at"],
                )
            except Exception as exc:
                if self._nonce:
                    try:
                        if journal_complete:
                            _restore_journal(self._path, self._backend, self._nonce)
                        else:
                            # No registry write can precede a complete journal.
                            self._path.unlink(missing_ok=True)
                        self._nonce = None
                        self._public["policy_restored"] = True
                    except Exception:
                        pass
                self._set("blocked", self._safe_error(exc), connected=False)
                raise TeamsConnectionError(self._safe_error(exc)) from None

    def connect_endpoint(self) -> str:
        with self._lock:
            if self._closed or not self._backend.supported or self._port is None:
                raise TeamsConnectionError("まず Teams デスクトップ接続を準備してください。")
            if self._public["expires_at"] is not None and self._clock() >= self._public["expires_at"]:
                self.restore()
                self._port = None
                self._set("needs_setup", "接続準備の有効時間が過ぎました。もう一度、接続を準備してください。",
                          connected=False, needs_restart=True, expires_at=None)
                raise TeamsConnectionError(self._public["detail"])
            was_connected = self._public["connected"]
            try:
                self._backend.verify_listener(self._port)
                self.restore()
                self._set(
                    "connected", "この Windows ユーザーの Teams に接続しました。一時設定は復元済みです。",
                    connected=True, needs_restart=False, debug_active=True, expires_at=None,
                )
                return f"http://127.0.0.1:{self._port}"
            except Exception as exc:
                if was_connected:
                    # We removed the temporary flags after connecting. A normal
                    # Teams restart therefore cannot reopen this old endpoint.
                    self._port = None
                    self._set("needs_setup", "Teams の接続が切れました。接続を準備し直してから、"
                              "会議後に Teams を完全に終了して開き直してください。",
                              connected=False, needs_restart=True)
                else:
                    self._set("waiting_for_restart", self._safe_error(exc), connected=False)
                raise TeamsConnectionError(self._public["detail"]) from None

    verified_endpoint = connect_endpoint

    def restore(self) -> dict:
        with self._lock:
            if self._nonce:
                try:
                    _restore_journal(self._path, self._backend, self._nonce)
                except Exception:
                    raise TeamsConnectionError("一時接続設定を復元できません。giji を再起動して再試行してください。") from None
                self._nonce = None
                self._public["policy_restored"] = True
            return self.status()

    def disconnect(self) -> dict:
        with self._lock:
            self.restore()
            may_be_active = self._port is not None or self._public["needs_restart"] or self._public["debug_active"]
            self._port = None
            return self._set(
                "disconnected", "接続設定を復元しました。Teams を完全に終了して開き直すと、"
                "起動中のデバッグ接続口も閉じます。",
                connected=False, needs_restart=may_be_active, expires_at=None,
            )

    def close(self) -> dict:
        with self._lock:
            result = self.disconnect()
            self._closed = True
            return result


def cleanup_main(args: list[str]) -> int:
    """Detached helper: restore our generation after parent exit or 10 minutes."""
    if len(args) != 4 or sys.platform != "win32":
        return 2
    backend = _WindowsBackend()
    ready = None
    try:
        path, pid, created, nonce = Path(args[0]), int(args[1]), float(args[2]), args[3]
        if path.name != JOURNAL_NAME or not path.is_absolute() or not re.fullmatch(r"[a-f0-9]{32}", nonce):
            return 2
        deadline = time.monotonic() + PREPARATION_TTL_SECONDS
        while True:
            journal = _read_journal(path, backend)
            if journal is None or journal["nonce"] != nonce:
                return 0
            if journal["pid"] != pid or journal["process_created"] != created:
                return 2
            if (
                not backend.parent_alive(pid, created) or time.time() >= journal["expires_at"]
                or time.monotonic() >= deadline
            ):
                _restore_journal(path, backend, nonce)
                return 0
            if ready is None:
                ready = _ready_path(path, nonce)
                _write_ready(ready, backend, nonce)
            time.sleep(1)
    except Exception:
        return 1
    finally:
        if ready is not None and _ready_matches(ready, backend, nonce):
            ready.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(cleanup_main(sys.argv[2:]) if sys.argv[1:2] == ["--teams-connection-cleanup"] else 2)
