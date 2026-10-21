"""Synthetic UIA observations, not captures from a real Microsoft Teams build."""

import builtins
import json
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import giji.teams_calendar_uia as uia
from giji.teams_calendar_parser import parse_calendar_snapshot
from giji.teams_calendar_uia import (
    MAX_OUTPUT,
    TeamsUIACalendar,
    TeamsUIACalendarError,
    is_teams_owned_process,
    snapshot_from_tree,
    worker_main,
)
from giji.teams_connection import ProcessIdentity


NOW = datetime(2026, 10, 21, 0, tzinfo=timezone.utc)
PACKAGE = "MSTeams_26000.1.2.3_x64__8wekyb3d8bbwe"


def node(name, kind="Pane", role="", children=None, **kwargs):
    return {"name": name, "kind": kind, "role": role, "children": children or [], **kwargs}


def calendar_tree(*events, english=False):
    return node("Microsoft Teams", "Window", children=[
        node("Calendar" if english else "カレンダー", "TabItem", "tab", selected=True),
        node("Today" if english else "今日", "Button", "button"),
        node("Day" if english else "日", "TabItem", "tab", selected=True),
        node("Calendar" if english else "予定表", "DataGrid", "grid", children=[
            node("October 21, 2026" if english else "2026年10月21日", "Text", "heading"),
        ] + [
            node(label, "Button", "button") for label in events
        ]),
    ])


def process_map():
    return {
        10: ProcessIdentity(10, "ms-teams.exe", rf"C:\Program Files\WindowsApps\{PACKAGE}\ms-teams.exe",
                            "same-sid", 1, PACKAGE),
        11: ProcessIdentity(11, "msedgewebview2.exe", r"C:\Runtime\msedgewebview2.exe", "same-sid", 10),
    }


def test_only_actual_same_user_teams_package_or_child_is_accepted():
    processes = process_map()
    assert is_teams_owned_process(10, processes, "same-sid")
    assert is_teams_owned_process(11, processes, "same-sid")
    assert not is_teams_owned_process(11, processes, "other-sid")
    assert not is_teams_owned_process(99, processes, "same-sid")


@pytest.mark.parametrize("field,value", [
    ("name", "chrome.exe"), ("owner", "other-sid"), ("package", "SpoofedTeams_123"),
    ("executable", r"C:\tmp\ms-teams.exe"), ("parent_pid", 11),
])
def test_spoofed_or_foreign_process_chain_is_rejected(field, value):
    processes = process_map()
    target = 11 if field == "parent_pid" else 10
    values = vars(processes[target]).copy()
    values[field] = value
    processes[target] = ProcessIdentity(**values)
    assert not is_teams_owned_process(11, processes, "same-sid")


@pytest.mark.parametrize("english,label,title", [
    (False, "定例会議、10:00〜11:00", "定例会議"),
    (True, "Planning, 10:00 AM to 11:00 AM", "Planning"),
])
def test_visible_calendar_names_parse_japanese_and_english(english, label, title):
    snapshot = snapshot_from_tree(calendar_tree(label, english=english), "Asia/Tokyo")
    result = parse_calendar_snapshot(snapshot, NOW)
    assert result["coverage"] == "visible_only"
    assert result["meetings"][0]["title"] == title
    assert result["meetings"][0]["start"] == "2026-10-21T01:00:00+00:00"
    assert result["meetings"][0]["end"] == "2026-10-21T02:00:00+00:00"
    assert result["meetings"][0]["join_url"] == ""


def test_calendar_tab_alone_does_not_read_chat_or_mini_date_picker():
    tree = node("Teams", "Window", children=[
        node("Calendar", "TabItem", "tab", selected=True),
        node("Today", "Button", "button"),
        node("Pick a date", "DataGrid", "grid", children=[node("21", "Button", "button")]),
        node("Chat", "List", "list", children=[node("Reminder 10:00-11:00", "Text", "text")]),
    ])
    with pytest.raises(TeamsUIACalendarError, match="日表示"):
        snapshot_from_tree(tree, "Asia/Tokyo")


def test_hidden_calendar_and_hidden_events_are_not_read():
    hidden = calendar_tree("Private, 10:00-11:00")
    hidden["visible"] = False
    with pytest.raises(TeamsUIACalendarError):
        snapshot_from_tree(hidden, "Asia/Tokyo")
    tree = calendar_tree("Hidden, 10:00-11:00", "Visible, 12:00-13:00")
    tree["children"][-1]["children"][1]["visible"] = False
    result = snapshot_from_tree(tree, "Asia/Tokyo")
    assert [event["label"] for event in result["events"]] == ["Visible, 12:00-13:00"]


def test_today_hint_is_not_evidence_of_displayed_date():
    tree = calendar_tree("Wrong day, 10:00-11:00")
    tree["children"][-1]["children"][0] = node("Today, October 21, 2026", "Button", "button")
    result = snapshot_from_tree(tree, "Asia/Tokyo")
    assert result["date_texts"] == []


def test_split_month_day_headers_are_observed_without_guessing_year():
    tree = calendar_tree("定例、10:00-11:00")
    tree["children"][-1]["children"][0]["name"] = "2026年10月"
    tree["children"][-1]["children"].insert(0, node("21 水曜日", "HeaderItem", "columnheader"))
    result = parse_calendar_snapshot(snapshot_from_tree(tree, "Asia/Tokyo"), NOW)
    assert result["meetings"][0]["start"] == "2026-10-21T01:00:00+00:00"


def test_mini_calendar_today_never_dates_undated_events_in_another_grid():
    tree = calendar_tree("Undated, 10:00-11:00")
    del tree["children"][-1]["children"][0]
    tree["children"].insert(0, node("Calendar", "DataGrid", "grid", children=[
        node("2026年10月21日", "Text", "heading"),
        node("2026年10月21日", "Button", "button", selected=True),
    ]))
    snapshot = snapshot_from_tree(tree, "Asia/Tokyo")
    assert snapshot["date_texts"] == []
    from giji.teams_calendar_parser import CalendarParseError
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot, NOW)


def test_outer_calendar_region_does_not_leak_picker_date_into_event_grid():
    tree = calendar_tree("Undated, 10:00-11:00")
    del tree["children"][-1]["children"][0]
    tree["children"].insert(0, node("2026年10月21日", "Button", "button"))
    tree["name"], tree["role"] = "Calendar", "main"
    assert snapshot_from_tree(tree, "Asia/Tokyo")["date_texts"] == []


def make_runner(snapshots=None, *, error=None, raw=None, returncode=0):
    payload = {"ok": error is None, "snapshots": snapshots or [], "error": error}
    return Mock(return_value=SimpleNamespace(
        stdout=raw if raw is not None else json.dumps(payload), stderr="PRIVATE native details", returncode=returncode,
    ))


def today_snapshot():
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    return {"calendar_present": True, "timezone": "UTC", "events": [
        {"title": "Meeting", "start": today + "T01:00:00+00:00", "end": today + "T02:00:00+00:00"},
    ]}


def test_reader_runs_only_isolated_worker_and_never_passes_credentials_or_settings():
    runner = make_runner([today_snapshot()])
    calendar = TeamsUIACalendar(runner=runner)
    assert calendar.fetch_today({"api_key": "PRIVATE", "_teams_call_state": "in_meeting"})
    args, kwargs = runner.call_args
    assert args[0] == [sys.executable, "-m", "giji.teams_calendar_uia", "--worker"]
    assert "PRIVATE" not in str((args, kwargs))
    assert 0 < kwargs["timeout"] <= 12
    assert calendar.status()["coverage"] == "visible_only"
    assert calendar.status()["connected"] is True


def test_frozen_reader_dispatch_uses_same_executable(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    runner = make_runner([today_snapshot()])
    TeamsUIACalendar(runner=runner).fetch_today({})
    assert runner.call_args.args[0] == [sys.executable, "--teams-uia-calendar-worker"]


@pytest.mark.parametrize("raw", ["private unparseable", "[]", "null", "{}", "x" * (MAX_OUTPUT + 1)],
                         ids=["invalid-json", "array", "null", "empty-object", "over-limit"])
def test_malformed_worker_output_never_reaches_public_error(raw):
    calendar = TeamsUIACalendar(runner=make_runner(raw=raw))
    with pytest.raises(TeamsUIACalendarError, match="読み取れません") as failure:
        calendar.fetch_today({})
    assert "private" not in str(failure.value)
    assert calendar.status()["connected"] is False


def test_native_error_text_is_not_trusted_as_public_error():
    calendar = TeamsUIACalendar(runner=make_runner(error="PRIVATE native contents", returncode=1))
    with pytest.raises(TeamsUIACalendarError) as failure:
        calendar.fetch_today({})
    assert "PRIVATE" not in str(failure.value)


def test_timeout_is_bounded_and_actionable_without_native_details():
    runner = Mock(side_effect=subprocess.TimeoutExpired("PRIVATE command", 12, output="PRIVATE calendar"))
    calendar = TeamsUIACalendar(runner=runner)
    with pytest.raises(TeamsUIACalendarError, match="時間切れ") as failure:
        calendar.fetch_today({})
    assert "PRIVATE" not in str(failure.value)


def test_snapshot_and_close_do_not_start_native_worker():
    runner = make_runner()
    calendar = TeamsUIACalendar(runner=runner)
    assert calendar.status()["state"] == "not_synced"
    calendar.close()
    with pytest.raises(TeamsUIACalendarError, match="終了"):
        calendar.fetch_today({})
    runner.assert_not_called()


def test_confirmed_empty_stays_partial_and_does_not_claim_complete_day():
    now = datetime.now(timezone.utc)
    snapshot = {"timezone": "UTC", "calendar_present": True, "day_view": True,
                "date_texts": [now.date().isoformat()], "events": [], "empty_text": "No events today"}
    calendar = TeamsUIACalendar(runner=make_runner([snapshot]))
    assert calendar.fetch_today({}) == []
    assert calendar.status()["coverage"] == "visible_only"


@pytest.mark.skipif(sys.platform == "win32", reason="Non-Windows fallback")
def test_unsupported_platform_never_starts_worker():
    calendar = TeamsUIACalendar()
    assert calendar.status()["state"] == "unavailable"
    with pytest.raises(TeamsUIACalendarError, match="Windows"):
        calendar.fetch_today({})


@pytest.mark.skipif(sys.platform == "win32", reason="Non-Windows fallback")
def test_worker_reports_only_error_code_on_unsupported_platform(capsys):
    assert worker_main() == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "platform"}


class FakeControl:
    def __init__(self, observed, pid=11):
        self.observed = observed
        self.element_info = SimpleNamespace(
            name=observed["name"], control_type=observed["kind"], visible=observed.get("visible", True),
            process_id=pid, element=SimpleNamespace(
                CurrentAriaRole=observed["role"],
                CurrentAriaProperties=observed.get("aria_properties", "selected=true" if observed.get("selected") else ""),
                CurrentIsControlElement=observed.get("is_control", True),
                CurrentIsContentElement=observed.get("is_content", True),
            ),
        )
        self.handle = observed.get("handle", 0)
        self.iface_window = SimpleNamespace(CurrentWindowVisualState=observed.get("window_state", 0))

    def children(self):
        return [FakeControl(child) for child in self.observed["children"]]

    def invoke(self):
        raise AssertionError("UIA reader must never invoke controls")

    def click_input(self):
        raise AssertionError("UIA reader must never click controls")

    def set_focus(self):
        raise AssertionError("UIA reader must never foreground Teams")

    def restore(self):
        raise AssertionError("UIA reader must never restore Teams")


def test_native_property_walker_is_read_only_hidden_aware_and_bounded():
    tree = calendar_tree("会議、10:00-11:00")
    tree["children"].insert(0, node("secret hidden", visible=False))
    result = uia._read_node(FakeControl(tree), [5])
    names = [item["name"] for item in uia._walk(result)]
    assert len(names) == 5
    assert "secret hidden" not in names
    assert result["children"][0]["selected"] is True


@pytest.mark.parametrize("message,category,module,suppressed", [
    ("Apply externally defined coinit_flags: 0", UserWarning, "pywinauto", True),
    ("Apply externally defined coinit_flags: 2", UserWarning, "pywinauto", False),
    ("Revert to STA COM threading mode", UserWarning, "pywinauto", False),
    ("Apply externally defined coinit_flags: 0", RuntimeWarning, "pywinauto", False),
    ("Apply externally defined coinit_flags: 0", UserWarning, "another_module", False),
])
def test_only_known_pywinauto_mta_import_warning_is_suppressed(
    monkeypatch, message, category, module, suppressed,
):
    actual_import = builtins.__import__
    desktop = object()

    def imported(name, *args, **kwargs):
        if name == "pywinauto":
            warnings.warn_explicit(message, category, "pywinauto/__init__.py", 71, module=module)
            return SimpleNamespace(Desktop=desktop)
        return actual_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", imported)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        assert uia._desktop_class() is desktop
        assert len(recorded) == (0 if suppressed else 1)
        # The import-only filter must not alter callers' warning policy.
        warnings.warn_explicit(
            "Apply externally defined coinit_flags: 0", UserWarning,
            "pywinauto/__init__.py", 71, module="pywinauto",
        )
        assert len(recorded) == (1 if suppressed else 2)


@pytest.mark.parametrize("background", [None, "minimized", "hidden"])
def test_native_collection_verifies_ownership_before_reading_calendar(monkeypatch, background):
    import giji.teams_connection as connection

    observed = calendar_tree("会議、10:00-11:00")
    if background:
        mark_offscreen(observed)
    observed["handle"] = 123
    owned = FakeControl(observed, 11)
    stranger = FakeControl(calendar_tree("PRIVATE other user, 12:00-13:00"), 99)
    identities = process_map()
    identities[99] = ProcessIdentity(99, "ms-teams.exe", r"C:\tmp\ms-teams.exe", "other-sid", 1, PACKAGE)
    backend = Mock()
    backend.owner.return_value = "same-sid"
    backend._process.side_effect = identities.__getitem__
    monkeypatch.setattr(connection, "_WindowsBackend", lambda: backend)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(process_iter=lambda _: [
        SimpleNamespace(info={"pid": 11, "name": "msedgewebview2.exe"}),
        SimpleNamespace(info={"pid": 99, "name": "ms-teams.exe"}),
    ]))
    desktop = Mock()
    desktop.windows.return_value = [stranger, owned]
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **_: desktop))
    monkeypatch.setitem(sys.modules, "tzlocal", SimpleNamespace(get_localzone_name=lambda: "Asia/Tokyo"))
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        IsWindow=lambda handle: handle == 123,
        IsIconic=lambda _: background == "minimized",
        IsWindowVisible=lambda _: background != "hidden",
    ))
    snapshots = uia.collect_snapshots()
    assert len(snapshots) == 1
    assert "PRIVATE" not in str(snapshots)
    assert snapshots[0]["background"] == background
    desktop.windows.assert_called_once_with(visible_only=False, enabled_only=False)
    backend.prepare.assert_not_called()
    backend.check_policy.assert_not_called()
    backend.write_value.assert_not_called()


def mark_offscreen(tree):
    tree["visible"] = False
    for child in tree.get("children", []):
        mark_offscreen(child)


@pytest.mark.parametrize("iconic,visible,expected", [
    (True, True, "minimized"), (True, False, "minimized"),
    (False, False, "hidden"), (False, True, None),
])
def test_native_window_classification_is_read_only(monkeypatch, iconic, visible, expected):
    window = FakeControl(node("Teams", "Window", handle=123))
    gui = SimpleNamespace(IsWindow=Mock(return_value=True), IsIconic=Mock(return_value=iconic),
                          IsWindowVisible=Mock(return_value=visible))
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    assert uia._window_background_state(window) == expected
    gui.IsWindow.assert_called_once_with(123)
    gui.IsIconic.assert_called_once_with(123)
    if not iconic:
        gui.IsWindowVisible.assert_called_once_with(123)


def test_dead_handle_and_offscreen_alone_do_not_authorize_background_bypass(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(IsWindow=lambda _: False))
    window = FakeControl(node("Teams", "Window", handle=123, visible=False))
    assert uia._window_background_state(window) is None
    assert uia._read_node(window, [100]) is None
    window.iface_window.CurrentWindowVisualState = 1
    assert uia._window_background_state(window) == "minimized"


@pytest.mark.parametrize("background", ["minimized", "hidden"])
def test_retained_background_calendar_can_be_read_without_foregrounding(background):
    observed = calendar_tree("会議、10:00-11:00")
    mark_offscreen(observed)
    assert uia._read_node(FakeControl(observed), [100]) is None
    tree = uia._read_node(FakeControl(observed), [100], allow_offscreen=True)
    tree["background"] = background
    snapshot = snapshot_from_tree(tree, "Asia/Tokyo")
    result = parse_calendar_snapshot(snapshot, NOW)
    assert snapshot["background"] == background
    assert result["meetings"][0]["start"] == "2026-10-21T01:00:00+00:00"


def test_background_ignores_explicit_aria_hidden_subtree_and_noncontent_leaf():
    observed = calendar_tree("会議、10:00-11:00")
    grid = observed["children"][-1]
    grid["children"].extend([
        node("Hidden group", aria_properties="hidden=true", children=[
            node("PRIVATE hidden meeting, 12:00-13:00", "Button", "button")]),
        node("PRIVATE raw meeting, 14:00-15:00", "Button", "button", is_control=False, is_content=False),
    ])
    mark_offscreen(observed)
    tree = uia._read_node(FakeControl(observed), [100], allow_offscreen=True)
    tree["background"] = "minimized"
    assert "PRIVATE" not in str(tree)
    snapshot = snapshot_from_tree(tree, "Asia/Tokyo")
    assert len(snapshot["events"]) == 1


@pytest.mark.parametrize("selected", [False, None])
def test_background_does_not_read_inactive_or_unconfirmed_calendar(selected):
    observed = calendar_tree("PRIVATE inactive calendar, 10:00-11:00")
    observed["children"][0]["selected"] = selected
    mark_offscreen(observed)
    tree = uia._read_node(FakeControl(observed), [100], allow_offscreen=True)
    tree["background"] = "hidden"
    with pytest.raises(TeamsUIACalendarError, match="バックグラウンド"):
        snapshot_from_tree(tree, "Asia/Tokyo")


def test_background_date_picker_still_cannot_supply_missing_event_date():
    observed = calendar_tree("Undated, 10:00-11:00")
    del observed["children"][-1]["children"][0]
    observed["children"].insert(0, node("Calendar", "DataGrid", "grid", children=[
        node("2026年10月21日", "Text", "heading"),
    ]))
    mark_offscreen(observed)
    tree = uia._read_node(FakeControl(observed), [100], allow_offscreen=True)
    tree["background"] = "minimized"
    snapshot = snapshot_from_tree(tree, "Asia/Tokyo")
    assert snapshot["date_texts"] == []


def test_background_provider_warns_about_suspended_refresh_and_missing_tree():
    snapshot = {**today_snapshot(), "background": "minimized"}
    calendar = TeamsUIACalendar(runner=make_runner([snapshot]))
    assert calendar.fetch_today({})
    assert calendar.status()["background"] is True
    assert "前面に出さず" in calendar.status()["detail"]
    assert any("更新が止まっている" in warning for warning in calendar.status()["warnings"])
    failed = TeamsUIACalendar(runner=make_runner(error="background", returncode=1))
    with pytest.raises(TeamsUIACalendarError, match="保存済みの予定は保持"):
        failed.fetch_today({})
    assert failed.status()["connected"] is False


def test_worker_suppresses_raw_native_exceptions_and_uses_mta(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "coinit_flags", 0, raising=False)
    com = SimpleNamespace(COINIT_MULTITHREADED=0, CoInitializeEx=Mock(), CoUninitialize=Mock())
    monkeypatch.setitem(sys.modules, "pythoncom", com)
    monkeypatch.setattr(uia, "collect_snapshots", Mock(side_effect=RuntimeError("PRIVATE window title")))
    assert worker_main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": False, "error": "unreadable"}
    assert "PRIVATE" not in output.err
    com.CoInitializeEx.assert_called_once_with(0)
    com.CoUninitialize.assert_called_once()
