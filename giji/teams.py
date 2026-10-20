"""Read-only Teams desktop call detection through Windows UI Automation.

The Teams local WebSocket API was retired in June 2026. The detector instead
observes the language-independent ``hangup-button`` automation id. It never
clicks controls, changes Teams settings, or treats failed UI reads as hangup.

UI Automation is a heuristic, not a supported Teams meeting lifecycle API.
The UI exposes neither a reliable meeting id nor participant speaker names.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypedDict


State = Literal["in_meeting", "idle", "unknown", "unavailable"]


class Detection(TypedDict):
    state: State
    detail: str


@dataclass(frozen=True)
class WindowObservation:
    """A small UI observation, deliberately without titles or chat contents."""

    automation_ids: frozenset[str]
    content_nodes: int
    has_document: bool
    failed: bool = False

    @property
    def inspectable(self) -> bool:
        # Native caption buttons alone are not evidence that the WebView
        # accessibility tree loaded. Require actual web content or several
        # app-specific ids.
        app_ids = {
            value
            for value in self.automation_ids
            if value
            and not value.isdecimal()
            and value.lower()
            not in {"minimize", "maximize", "restore", "close"}
        }
        return not self.failed and (
            (self.has_document and self.content_nodes >= 6) or len(app_ids) >= 3
        )


def classify_observations(
    observations: list[WindowObservation], *, teams_running: bool
) -> Detection:
    """Keep an unreadable/minimized meeting window from becoming false idle."""
    if any("hangup-button" in item.automation_ids for item in observations):
        return {"state": "in_meeting", "detail": "Teams の参加状態を検知しました。"}
    if not teams_running and not observations:
        return {"state": "idle", "detail": "Teams デスクトップは起動していません。"}
    if not observations or any(not item.inspectable for item in observations):
        return {
            "state": "unknown",
            "detail": "Teams の状態を読み取れません。Teams の会議画面を開いてください。"
            "録音中の場合は継続します。必要に応じて手動で停止できます。",
        }
    return {
        "state": "idle",
        "detail": "Teams の画面を確認しました。参加中の通話は検知していません。",
    }


def _scan_windows() -> Detection:
    # Imports must stay lazy: the web UI, docs and unit tests also run on Linux.
    import psutil
    from pywinauto import Desktop

    teams_names = {"ms-teams.exe", "msteams.exe", "teams.exe"}
    teams_pids: set[int] = set()
    for process in psutil.process_iter(["pid", "name"]):
        name = process.info.get("name")
        if name and name.lower() in teams_names:
            teams_pids.add(process.info["pid"])

    if not teams_pids:
        return classify_observations([], teams_running=False)

    windows = Desktop(backend="uia").windows(
        visible_only=False, enabled_only=False
    )
    observations: list[WindowObservation] = []
    for window in windows:
        try:
            info = window.element_info
            # Teams' WebView renderer may be owned by msedgewebview2.exe, so
            # the known root class is also accepted. Never scan browser tabs.
            if (
                info.class_name != "TeamsWebView"
                and info.process_id not in teams_pids
            ):
                continue
            # Enumerating the UIA subtree requests accessibility information;
            # no foreground activation or machine-wide setting is required.
            controls = window.descendants()
            ids: set[str] = set()
            content_nodes = 0
            has_document = False
            for control in controls:
                element = control.element_info
                automation_id = element.automation_id or ""
                if automation_id:
                    ids.add(automation_id)
                if automation_id == "hangup-button":
                    return {
                        "state": "in_meeting",
                        "detail": "Teams の参加状態を検知しました。",
                    }
                kind = element.control_type
                has_document = has_document or kind == "Document"
                if kind not in {"Window", "TitleBar", "Border", "Pane"}:
                    content_nodes += 1
            observations.append(
                WindowObservation(frozenset(ids), content_nodes, has_document)
            )
        except Exception:
            # Windows can disappear during enumeration or deny UIA access.
            # Even when another Teams window is readable, this one could be
            # the current meeting and therefore must not count as a hangup.
            observations.append(WindowObservation(frozenset(), 0, False, True))
    return classify_observations(observations, teams_running=True)


class TeamsDetector:
    """Bounded polling, with all COM/UIA work confined to one daemon thread.

    A hung third-party UIA provider cannot block the recorder or shutdown. A
    subsequent poll reuses the pending request instead of spawning threads.
    ``scanner`` is an injectable seam for deterministic tests.
    """

    def __init__(
        self,
        scanner: Callable[[], Detection] | None = None,
        *,
        timeout_seconds: float = 1.0,
    ) -> None:
        self._scanner = scanner or _scan_windows
        self._native = scanner is None
        self._timeout = max(0.01, timeout_seconds)
        self._request = threading.Event()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._poll_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending = False
        self._result: Detection | None = None
        self._completed_at = 0.0

    def poll(self) -> Detection:
        if self._native and sys.platform != "win32":
            return {
                "state": "unavailable",
                "detail": "Teams の自動検知は Windows 11 デスクトップ版に対応しています。",
            }
        if self._closed.is_set():
            return {"state": "unavailable", "detail": "Teams の監視を終了しました。"}
        with self._poll_lock:
            if (
                self._thread is not None
                and not self._thread.is_alive()
                and self._result is not None
            ):
                # A missing native dependency is a persistent, actionable
                # unavailable state, not a sequence of later UIA timeouts.
                return dict(self._result)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="giji-teams-uia", daemon=True
                )
                self._thread.start()
            if not self._pending:
                self._ready.clear()
                self._pending = True
                self._request.set()
            if not self._ready.wait(self._timeout):
                return {
                    "state": "unknown",
                    "detail": "Teams の状態確認に時間がかかっています。録音は継続します。",
                }
            self._pending = False
            if time.monotonic() - self._completed_at > 10:
                return {
                    "state": "unknown",
                    "detail": "Teams の状態を再確認しています。録音は継続します。",
                }
            return dict(self._result or {"state": "unknown", "detail": "確認中です。"})

    def _run(self) -> None:
        pythoncom = None
        if self._native:
            try:
                import pythoncom as com

                com.CoInitializeEx(com.COINIT_MULTITHREADED)
                pythoncom = com
            except ImportError:
                self._result = {
                    "state": "unavailable",
                    "detail": "Windows 用の検知ライブラリが未導入です。セットアップを実行してください。",
                }
                self._completed_at = time.monotonic()
                self._ready.set()
                return
            except Exception:
                self._result = {
                    "state": "unknown",
                    "detail": "Windows の画面検知を開始できませんでした。アプリを再起動してください。",
                }
                self._completed_at = time.monotonic()
                self._ready.set()
                return
        try:
            while not self._closed.is_set():
                self._request.wait()
                self._request.clear()
                if self._closed.is_set():
                    break
                try:
                    result = self._scanner()
                except ImportError:
                    result = {
                        "state": "unavailable",
                        "detail": "Windows 用の検知ライブラリが未導入です。セットアップを実行してください。",
                    }
                except Exception:
                    result = {
                        "state": "unknown",
                        "detail": "Teams の画面を確認できません。録音は継続します。"
                        "Teams と本アプリを同じ Windows ユーザーで起動してください。",
                    }
                self._result = result
                self._completed_at = time.monotonic()
                self._ready.set()
        finally:
            if pythoncom is not None:
                pythoncom.CoUninitialize()

    def close(self) -> None:
        self._closed.set()
        self._request.set()
