"""Read retained Teams calendar accessibility information without changing Teams.

The Windows UI Automation provider is used only for property reads. This preview
does not navigate, invoke controls, start Teams, enable debugging, or obtain
credentials. UIA executes in an isolated process so a stalled WebView provider
cannot block recording. Minimized/hidden owned windows can be read when Teams
retains their accessible calendar; an absent/suspended tree cannot be recreated
without changing the application. No real Teams capture is bundled here.
"""

from __future__ import annotations

import json
import ntpath
import re
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable, Mapping

from .teams_calendar_parser import CalendarParseError, parse_calendar_snapshot
from .teams_connection import ProcessIdentity


MAX_NODES = 6000
MAX_DEPTH = 36
MAX_OUTPUT = 4 * 1024 * 1024
_PACKAGE = re.compile(r"MSTeams_[0-9.]+_(?:x64|x86|arm64|neutral)__8wekyb3d8bbwe", re.I)
_CALENDAR = re.compile(r"(?:\bcalendar\b|予定表|カレンダー)", re.I)
_TODAY = re.compile(r"^(?:today|今日|本日)$", re.I)
_DAY = re.compile(r"^(?:day|日|日表示)$", re.I)
_RANGE = re.compile(r"\d{1,2}(?::\d{2}|時).*?(?:to|until|から|[〜～–—-]).*?\d{1,2}(?::\d{2}|時)", re.I)
_DATE_HEADER = re.compile(
    r"\d{4}年|\d{4}[-/]\d|\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|\d{1,2}月\d{1,2}日",
    re.I,
)
_EMPTY = re.compile(
    r"^(?:No (?:events|meetings)(?: scheduled| today| for today)?|There are no (?:events|meetings)(?: scheduled| today| for today)?|Nothing scheduled(?: today| for today)?|(?:今日|本日)の(?:予定|イベント|会議)はありません|(?:今日|本日)は(?:予定|イベント|会議)がありません|(?:予定|イベント|会議)(?:は|が)ありません)[.!。]?$",
    re.I,
)
_GUIDANCE = "Teams のカレンダーで、今日の予定を一度日表示にしてください。取得できる画面情報が残っていれば、最小化中も同期します。"
_BACKGROUND_GUIDANCE = (
    "バックグラウンドの Teams から予定表の画面情報を確認できません。"
    "必要なら Teams で今日の予定表を一度開いてください。"
    "最小化・トレイ収納で情報が破棄される環境では取得できません。保存済みの予定は保持されます。"
)
_BACKGROUND_WARNING = (
    "Teams を前面に出さず、保持されている画面情報を読み取っています。"
    "バックグラウンドで更新が止まっている変更は反映されない場合があります。"
)
_ERRORS = {
    "platform": "Teams の画面連携は Windows 11 で利用できます。",
    "not_running": "サインイン済みの Teams デスクトップを開いてください。Outlook は不要です。",
    "unreadable": "Teams の画面を読み取れません。同じ Windows ユーザーで Teams を開いてください。",
    "calendar": _GUIDANCE,
    "background": _BACKGROUND_GUIDANCE,
    "dependencies": "Windows 用の画面読み取りライブラリを利用できません。配布 ZIP を展開し直してください。",
    "worker": "Teams の画面読み取りを開始できませんでした。アプリを開き直してください。",
}


class TeamsUIACalendarError(RuntimeError):
    """A public error without raw COM exceptions or observed screen contents."""


def is_teams_owned_process(pid: int, processes: Mapping[int, ProcessIdentity], owner: str) -> bool:
    """Require an actual current-user MSTeams package, not a matching window name."""
    visited = set()
    for _ in range(24):
        process = processes.get(pid)
        if (process is None or pid in visited or process.owner != owner
                or ntpath.basename(process.executable).lower() != process.name.lower()):
            return False
        visited.add(pid)
        if process.name.lower() == "ms-teams.exe":
            return bool(_PACKAGE.fullmatch(process.package)
                        and ntpath.basename(ntpath.dirname(process.executable)).lower() == process.package.lower())
        if process.name.lower() != "msedgewebview2.exe":
            return False
        pid = process.parent_pid
    return False


def _walk(tree: dict):
    """Bound synthetic and native observation trees alike."""
    pending = [(tree, 0)]
    count = 0
    while pending and count < MAX_NODES:
        node, depth = pending.pop()
        if not isinstance(node, dict) or node.get("visible", True) is not True:
            continue
        count += 1
        yield node
        children = node.get("children", [])
        if depth < MAX_DEPTH and isinstance(children, list):
            pending.extend((child, depth + 1) for child in reversed(children[:MAX_NODES]))


def _name(node: dict) -> str:
    value = node.get("name")
    return re.sub(r"\s+", " ", value[:1500]).strip() if isinstance(value, str) else ""


def _is_event(node: dict) -> bool:
    return bool(_RANGE.search(_name(node))
                and (node.get("role") in {"button", "gridcell", "listitem", "link", "row"}
                     or node.get("kind") in {"Button", "DataItem", "ListItem", "Hyperlink"}))


def _has_calendar_content(node: dict) -> bool:
    return any(_is_event(child) or _EMPTY.fullmatch(_name(child)) for child in _walk(node))


def snapshot_from_tree(tree: dict, timezone_name: str) -> dict:
    """Extract calendar observations from a public UIA property tree.

    A Calendar navigation tab alone is insufficient. Prefer a named calendar
    landmark; otherwise require selected Calendar, Today and a time-bearing
    grid/list. A mini date picker or a chat named Calendar cannot establish it.
    """
    nodes = list(_walk(tree))
    selected_calendar = any(
        _CALENDAR.fullmatch(_name(node)) and node.get("selected") is True
        and (node.get("role") in {"tab", "treeitem", "button"}
             or node.get("kind") in {"TabItem", "TreeItem", "Button"})
        for node in nodes
    )
    background = tree.get("background") if isinstance(tree, dict) else None
    if background in {"minimized", "hidden"} and not selected_calendar:
        # Offscreen alone cannot distinguish a minimized app from an inactive
        # Calendar page cached behind Chat. Require the active navigation state.
        raise TeamsUIACalendarError(_BACKGROUND_GUIDANCE)
    has_today = any(_TODAY.fullmatch(_name(node))
                    and (node.get("role") == "button" or node.get("kind") == "Button") for node in nodes)
    roots = [node for node in nodes if _CALENDAR.search(_name(node))
             and (node.get("role") in {"main", "region", "grid", "list"}
                  or node.get("kind") in {"DataGrid", "Table", "List"})
             and _has_calendar_content(node)]
    if not roots and selected_calendar and has_today:
        roots = [node for node in nodes
                 if (node.get("role") == "grid" or node.get("kind") in {"DataGrid", "Table"})
                 and _has_calendar_content(node)]
    if not roots:
        raise TeamsUIACalendarError(_GUIDANCE)
    # Prefer the actual event grid over an outer Calendar region that also
    # contains a mini date picker. Never combine that picker's selected today
    # with undated events in a different grid.
    content_grids = [node for node in nodes
                     if (node.get("role") == "grid" or node.get("kind") in {"DataGrid", "Table"})
                     and _has_calendar_content(node)
                     and any(any(child is node for child in _walk(root)) for root in roots)]
    if content_grids:
        roots = content_grids
    root_ids = {id(root) for root in roots}
    roots = [root for root in roots if not any(id(child) in root_ids and child is not root
                                             for child in _walk(root))]
    calendar_nodes = []
    seen_nodes = set()
    for root in roots:
        for node in _walk(root):
            if id(node) not in seen_nodes:
                calendar_nodes.append(node)
                seen_nodes.add(id(node))
    events = []
    labels = set()
    for node in calendar_nodes:
        label = _name(node)
        if label not in labels and _is_event(node):
            # Runtime UIA ids are not stable appointment identifiers. The parser
            # derives an observation id and the service merges partial coverage.
            labels.add(label)
            events.append({"label": label})
    headers = [node for node in calendar_nodes if node.get("role") in {"heading", "columnheader"}
               or node.get("kind") == "HeaderItem"]
    headers.extend(root for root in roots if _DATE_HEADER.search(_name(root)) and not _RANGE.search(_name(root)))
    # A date-navigation control is useful only inside the verified content
    # subtree. Sidebar date-picker buttons and Today hints are never evidence.
    headers.extend(node for node in calendar_nodes
                   if node.get("kind") == "Button" and len(_name(node)) <= 100
                   and _DATE_HEADER.search(_name(node)) and not _RANGE.search(_name(node))
                   and not re.search(r"today|今日|本日", _name(node), re.I))
    date_texts = list(dict.fromkeys(_name(node) for node in headers if _name(node)))[:200]
    day_columns = [_name(node) for node in calendar_nodes
                   if node.get("role") == "columnheader" or node.get("kind") == "HeaderItem"][:200]
    day_view = any(_DAY.fullmatch(_name(node)) and node.get("selected") is True for node in nodes)
    empty = next((_name(node) for node in calendar_nodes if _EMPTY.fullmatch(_name(node))), "")
    return {"calendar_present": True, "timezone": timezone_name, "date_texts": date_texts,
            "day_column_texts": day_columns, "day_view": day_view, "events": events[:500],
            "empty_text": empty, "truncated": len(nodes) >= MAX_NODES or len(events) > 500,
            "background": background if background in {"minimized", "hidden"} else None}


def _read_node(control, budget: list[int], depth: int = 0, *, allow_offscreen: bool = False) -> dict | None:
    if budget[0] <= 0 or depth > MAX_DEPTH:
        return None
    info = control.element_info
    if not allow_offscreen and not info.visible:
        return None
    budget[0] -= 1
    element = info.element
    try:
        role = str(element.CurrentAriaRole or "").lower()
    except Exception:
        role = ""
    try:
        properties = str(element.CurrentAriaProperties or "").lower()
    except Exception:
        properties = ""
    if re.search(r"(?:^|;)\s*(?:aria-)?hidden=true(?:;|$)", properties):
        return None
    exposed = True
    if allow_offscreen:
        # IsOffscreen is true for the whole minimized window, including its
        # active page. Control/content membership still distinguishes exposed
        # accessibility nodes from raw structural or ignored DOM nodes.
        try:
            exposed = bool(element.CurrentIsControlElement or element.CurrentIsContentElement)
        except Exception:
            exposed = False
    selected = bool(re.search(r"(?:^|;)\s*(?:selected|pressed|checked)=true(?:;|$)", properties))
    if not selected and info.control_type in {"TabItem", "TreeItem", "RadioButton", "Button", "ListItem"}:
        try:
            selected = bool(control.iface_selection_item.CurrentIsSelected)
        except Exception:
            pass
    node = {"name": str(info.name or "")[:1500] if exposed else "", "kind": info.control_type,
            "role": role if exposed else "", "selected": selected if exposed else False,
            "visible": True, "children": []}
    if depth < MAX_DEPTH:
        for child in control.children():
            if budget[0] <= 0:
                break
            observed = _read_node(child, budget, depth + 1, allow_offscreen=allow_offscreen)
            if observed is not None:
                node["children"].append(observed)
    return node


def _window_background_state(window) -> str | None:
    """Query native window state without activation, restoration or visibility writes.

    UIA IsOffscreen alone is insufficient evidence: it is also true for hidden
    DOM branches. The exception is permitted only for a proven native minimized
    or hidden top-level window after its Teams ownership has been verified.
    """
    try:
        handle = int(window.handle or 0)
    except (AttributeError, TypeError, ValueError):
        handle = 0
    if handle:
        try:
            import win32gui

            if win32gui.IsWindow(handle):
                if win32gui.IsIconic(handle):
                    return "minimized"
                if not win32gui.IsWindowVisible(handle):
                    return "hidden"
                return None
        except Exception:
            pass
    try:
        # UIA WindowVisualState_Minimized is 1. Other states do not prove that
        # invisible descendants belong to an active background calendar.
        if int(window.iface_window.CurrentWindowVisualState) == 1:
            return "minimized"
    except Exception:
        pass
    return None


def _desktop_class():
    # pywinauto 0.6.9 announces an explicitly chosen MTA with a UserWarning.
    # This is our intended worker setup; keep every other import diagnostic,
    # especially its warning about falling back to an incompatible STA.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=r"\AApply externally defined coinit_flags: 0\Z",
            category=UserWarning, module=r"\Apywinauto\Z",
        )
        from pywinauto import Desktop
    return Desktop


def collect_snapshots() -> list[dict]:
    """Native read path, invoked only by the bounded Windows worker process."""
    import psutil
    from tzlocal import get_localzone_name

    from .teams_connection import _WindowsBackend

    Desktop = _desktop_class()
    backend = _WindowsBackend()
    owner = backend.owner()
    possible = {p.info["pid"] for p in psutil.process_iter(["pid", "name"])
                if (p.info.get("name") or "").lower() in {"ms-teams.exe", "msedgewebview2.exe"}}
    if not possible:
        raise TeamsUIACalendarError(_ERRORS["not_running"])
    process_cache = {}
    snapshots = []
    had_owned_window = False
    had_background_window = False
    zone = get_localzone_name()
    for window in Desktop(backend="uia").windows(visible_only=False, enabled_only=False):
        try:
            pid = window.element_info.process_id
            if pid not in possible:
                continue
            current = pid
            for _ in range(24):
                if not current or current in process_cache:
                    break
                process = backend._process(current)
                process_cache[current] = process
                if process.name.lower() != "msedgewebview2.exe":
                    break
                current = process.parent_pid
            if not is_teams_owned_process(pid, process_cache, owner):
                continue
            had_owned_window = True
            background = _window_background_state(window)
            had_background_window = had_background_window or background is not None
            tree = _read_node(window, [MAX_NODES], allow_offscreen=background is not None)
            if tree:
                tree["background"] = background
                snapshots.append(snapshot_from_tree(tree, zone))
            if len(snapshots) >= 4:
                break
        except Exception:
            # A chat/popout/unreadable window is not an empty calendar. Another
            # owned window may still expose the requested calendar.
            continue
    if not snapshots:
        raise TeamsUIACalendarError(_BACKGROUND_GUIDANCE if had_background_window
                                    else _GUIDANCE if had_owned_window else _ERRORS["unreadable"])
    return snapshots


def worker_main() -> int:
    if sys.platform != "win32":
        print(json.dumps({"ok": False, "error": "platform"}))
        return 1
    pythoncom = None
    try:
        sys.coinit_flags = 0  # pywinauto and pythoncom must share this main MTA.
        import pythoncom as com

        com.CoInitializeEx(com.COINIT_MULTITHREADED)
        pythoncom = com
        result = {"ok": True, "snapshots": collect_snapshots()}
        encoded = json.dumps(result, ensure_ascii=True)
        if len(encoded) > MAX_OUTPUT:
            raise TeamsUIACalendarError(_ERRORS["unreadable"])
        print(encoded)
        return 0
    except ImportError:
        result = {"ok": False, "error": "dependencies"}
    except TeamsUIACalendarError as exc:
        code = next((key for key, value in _ERRORS.items() if value == str(exc)), "unreadable")
        result = {"ok": False, "error": code}
    except Exception:
        result = {"ok": False, "error": "unreadable"}
    finally:
        if pythoncom is not None:
            pythoncom.CoUninitialize()
    print(json.dumps(result))
    return 1


class TeamsUIACalendar:
    def __init__(self, timeout_seconds: float = 12, *, runner: Callable | None = None):
        self._runner = runner or subprocess.run
        self._native = runner is None
        self._timeout = max(0.1, min(timeout_seconds, 30))
        self._lock = threading.Lock()
        self._closed = False
        self._public = {"provider": "teams_desktop", "state": "not_synced", "connected": False,
                        "detail": _GUIDANCE, "coverage": "unknown", "warnings": []}

    def status(self, settings: dict | None = None) -> dict:
        result = {**self._public, "warnings": list(self._public["warnings"])}
        if self._native and sys.platform != "win32":
            result.update(state="unavailable", connected=False, detail=_ERRORS["platform"])
        return result

    def fetch_today(self, settings: dict | None = None) -> list[dict]:
        if self._closed:
            raise TeamsUIACalendarError("Teams の画面読み取りを終了しました。")
        if self._native and sys.platform != "win32":
            raise TeamsUIACalendarError(_ERRORS["platform"])
        started = time.monotonic()
        if not self._lock.acquire(timeout=self._timeout):
            raise TeamsUIACalendarError("Teams の画面を読み取り中です。しばらくして同期してください。")
        try:
            command = ([sys.executable, "--teams-uia-calendar-worker"] if getattr(sys, "frozen", False)
                       else [sys.executable, "-m", "giji.teams_calendar_uia", "--worker"])
            result = self._runner(
                command, capture_output=True, text=True, encoding="utf-8", check=False,
                timeout=max(0.1, self._timeout - (time.monotonic() - started)),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if not isinstance(result.stdout, str) or len(result.stdout) > MAX_OUTPUT:
                raise TeamsUIACalendarError(_ERRORS["unreadable"])
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict):
                raise TeamsUIACalendarError(_ERRORS["unreadable"])
            if result.returncode or payload.get("ok") is not True:
                raise TeamsUIACalendarError(_ERRORS.get(str(payload.get("error")), _ERRORS["unreadable"]))
            snapshots = payload.get("snapshots")
            if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 4:
                raise TeamsUIACalendarError(_ERRORS["unreadable"])
            meetings = {}
            warnings = []
            parsed_any = False
            background_read = False
            for snapshot in snapshots:
                try:
                    parsed = parse_calendar_snapshot(snapshot)
                    parsed_any = True
                    background_read = background_read or snapshot.get("background") in {"minimized", "hidden"}
                    meetings.update({meeting["id"]: meeting for meeting in parsed["meetings"]})
                    warnings.extend(warning for warning in parsed["warnings"]
                                    if "ブラウザーのタイムゾーン" not in warning)
                except CalendarParseError:
                    warnings.append("日時を確認できない予定表の表示がありました。保存済みの予定は保持されます。")
            if not parsed_any:
                raise TeamsUIACalendarError(_GUIDANCE)
            warnings += ["画面で確認した予定のみです。画面外の予定や後から変更・取消された予定は再確認が必要です。",
                         "表示時刻は Windows のタイムゾーンで解釈します。Teams の表示地域と一致させてください。"]
            if background_read:
                warnings.append(_BACKGROUND_WARNING)
            self._public.update(state="connected", connected=True, coverage="visible_only",
                                detail=("Teams を前面に出さず、保持されている今日の予定情報を読み取りました。"
                                        if background_read and meetings else
                                        "Teams の予定情報を読み取りました。Outlook や追加登録は不要です。"
                                        if meetings else "Teams から取得できた範囲には今日の予定がありません。"),
                                background=background_read,
                                warnings=list(dict.fromkeys(warnings)))
            return sorted(meetings.values(), key=lambda meeting: (meeting["start"], meeting["id"]))
        except subprocess.TimeoutExpired:
            error = "Teams の画面読み取りが時間切れになりました。Teams の予定表を表示して再試行してください。録音は継続します。"
            self._public.update(state="error", connected=False, detail=error, coverage="unknown", background=False)
            raise TeamsUIACalendarError(error) from None
        except Exception as exc:
            error = str(exc) if isinstance(exc, TeamsUIACalendarError) else _ERRORS["unreadable"]
            self._public.update(state="error", connected=False, detail=error, coverage="unknown", background=False)
            raise TeamsUIACalendarError(error) from None
        finally:
            self._lock.release()

    def close(self) -> None:
        self._closed = True
        self._public.update(state="unavailable", connected=False, background=False,
                            detail="Teams の画面読み取りを終了しました。")


if __name__ == "__main__":
    raise SystemExit(worker_main() if sys.argv[1:] == ["--worker"] else 2)
