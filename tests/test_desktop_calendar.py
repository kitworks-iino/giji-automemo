import json
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest

from giji.desktop_calendar import (
    EXCEPTION_REPLACE_TIME,
    HTML_BODY,
    DesktopCalendarError,
    DesktopOutlookCalendar,
    collect_today,
    extract_teams_url,
    local_day_bounds,
    normalize_appointment,
    windows_filter_datetime,
    worker_main,
)


JOIN = "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_test/0?context=%7B%7D"
TODAY = datetime(2026, 10, 20, 12, tzinfo=ZoneInfo("Asia/Tokyo"))


def appointment(**changes):
    value = {
        "Class": 26,
        "Subject": "定例会議",
        "StartUTC": datetime(2026, 10, 20, 1),
        "EndUTC": datetime(2026, 10, 20, 2),
        "GlobalAppointmentID": "global-appointment",
        "EntryID": "entry",
        "RecurrenceState": 0,
        "MeetingStatus": 1,
        "Location": "",
        "Body": "会議へ参加する\n" + JOIN,
        "HTMLBody": "",
        "RequiredAttendees": "田中; 佐藤; 田中",
        "OptionalAttendees": "伊藤; 佐藤",
        "Organizer": "主催者",
        "PropertyAccessor": Mock(),
    }
    value.update(changes)
    return SimpleNamespace(**value)


class FakeItems:
    def __init__(self, appointments):
        self.appointments = appointments
        self.calls = []
        self.cursor = 0

    @property
    def Count(self):
        raise AssertionError("Count must not be read on an expanded recurring collection")

    def Sort(self, field, descending=False):
        self.calls.append(("sort", field, descending))

    @property
    def IncludeRecurrences(self):
        return True

    @IncludeRecurrences.setter
    def IncludeRecurrences(self, value):
        self.calls.append(("recurrences", value))

    def Restrict(self, query):
        self.calls.append(("restrict", query))
        return self

    def GetFirst(self):
        self.cursor = 0
        return self.appointments[0] if self.appointments else None

    def GetNext(self):
        self.cursor += 1
        return self.appointments[self.cursor] if self.cursor < len(self.appointments) else None


def namespace_for(appointments):
    items = FakeItems(appointments)
    namespace = Mock()
    namespace.CurrentProfileName = "既存の Outlook プロファイル"
    namespace.GetDefaultFolder.return_value = SimpleNamespace(Items=items, StoreID="store-id")
    return namespace, items


def test_extracts_only_allowlisted_meeting_paths_and_decodes_html():
    assert extract_teams_url("会議 <" + JOIN + ">.") == JOIN
    short = "https://teams.cloud.microsoft/meet/123?p=hello&b=world"
    assert extract_teams_url(f'<a href="{short.replace("&", "&amp;")}">参加</a>') == short
    assert extract_teams_url("https://teams.microsoft.com/downloads " + JOIN) == JOIN
    for unsafe in (
        "https://teams.microsoft.com.evil.example/meet/123",
        "https://teams.microsoft.com@evil.example/meet/123",
        "https://evil.example/meet/123",
        "https://teams.microsoft.com:444/meet/123",
        "https://teams.microsoft.com/",
        "http://teams.microsoft.com/meet/123",
    ):
        assert extract_teams_url(unsafe) is None


def test_safe_links_embedded_target_is_validated_without_following_redirects():
    safe = "https://apc01.safelinks.protection.outlook.com/?url=" + quote(JOIN, safe="") + "&data=test"
    assert extract_teams_url(safe) == JOIN
    evil = "https://apc01.safelinks.protection.outlook.com/?url=" + quote("https://evil.example/meet/123", safe="")
    assert extract_teams_url(evil) is None
    spoof = "https://safelinks.protection.outlook.com.evil.example/?url=" + quote(JOIN, safe="")
    assert extract_teams_url(spoof) is None


@pytest.mark.parametrize("day,hours", [(datetime(2026, 3, 8), 23), (datetime(2026, 11, 1), 25)])
def test_local_day_boundaries_keep_daylight_saving_transition(day, hours):
    start, end = local_day_bounds(day.replace(tzinfo=ZoneInfo("America/New_York")))
    assert (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() == hours * 3600


def test_collect_sort_expand_restrict_order_and_local_filter_format():
    ns, items = namespace_for([appointment()])

    def format_date(value):
        return value.strftime("%Y/%m/%d %H:%M")

    meetings, account = collect_today(ns, now=TODAY, date_formatter=format_date)
    ns.GetDefaultFolder.assert_called_once_with(9)
    assert items.calls == [
        ("sort", "[Start]", False),
        ("recurrences", True),
        ("restrict", "[Start] < '2026/10/21 00:00' AND [End] > '2026/10/20 00:00'"),
    ]
    assert meetings[0]["start"] == "2026-10-20T01:00:00+00:00"
    assert meetings[0]["attendees"] == ["田中", "佐藤", "伊藤"]
    assert account == "既存の Outlook プロファイル"


def test_windows_formatter_preserves_local_clock_through_pywin32_utc_conversion(monkeypatch):
    def date_format(locale, flags, value, pattern):
        assert locale == 0x0400
        assert flags == 1
        assert pattern is None
        # Emulate pywin32 PyWinObject_AsSYSTEMTIME, which converts to UTC.
        return value.astimezone(timezone.utc).strftime("%Y/%m/%d")

    def time_format(locale, flags, value, pattern):
        assert flags == 2  # TIME_NOSECONDS
        return value.astimezone(timezone.utc).strftime("%H:%M")

    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(
        GetDateFormat=date_format, GetTimeFormat=time_format,
    ))
    midnight = datetime(2026, 10, 20, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert windows_filter_datetime(midnight) == "2026/10/20 00:00"


def test_overlap_boundaries_and_non_teams_events_are_filtered():
    ns, _ = namespace_for([
        appointment(GlobalAppointmentID="overnight", StartUTC=datetime(2026, 10, 19, 14),
                    EndUTC=datetime(2026, 10, 19, 16)),
        appointment(GlobalAppointmentID="ended-midnight", StartUTC=datetime(2026, 10, 19, 14),
                    EndUTC=datetime(2026, 10, 19, 15)),
        appointment(GlobalAppointmentID="tomorrow", StartUTC=datetime(2026, 10, 20, 15),
                    EndUTC=datetime(2026, 10, 20, 16)),
        appointment(GlobalAppointmentID="ordinary", Body="オフライン会議", PropertyAccessor=Mock(GetProperty=Mock(return_value=""))),
    ])
    meetings, _ = collect_today(ns, now=TODAY, date_formatter=lambda d: d.isoformat())
    assert len(meetings) == 1
    assert meetings[0]["start"] == "2026-10-19T14:00:00+00:00"


def test_recurring_occurrences_have_distinct_stable_ids_and_duplicate_results_collapse():
    first = appointment(RecurrenceState=2)
    second = appointment(RecurrenceState=2, StartUTC=datetime(2026, 10, 20, 3),
                         EndUTC=datetime(2026, 10, 20, 4))
    ns, _ = namespace_for([first, second, first])
    meetings, _ = collect_today(ns, now=TODAY, date_formatter=lambda d: d.isoformat())
    assert len(meetings) == 2
    assert meetings[0]["id"] != meetings[1]["id"]
    assert normalize_appointment(first, "store-id")["id"] == meetings[0]["id"]


def test_moved_nonrecurring_appointment_keeps_id():
    before = normalize_appointment(appointment(), "store")
    after = normalize_appointment(appointment(StartUTC=datetime(2026, 10, 20, 3),
                                             EndUTC=datetime(2026, 10, 20, 4)), "store")
    assert before["id"] == after["id"]


def test_rescheduled_recurring_exception_keeps_original_occurrence_id():
    before = normalize_appointment(appointment(RecurrenceState=2), "store")
    accessor = Mock()
    accessor.GetProperty.return_value = datetime(2026, 10, 20, 1)
    changed = appointment(
        RecurrenceState=3, StartUTC=datetime(2026, 10, 20, 3), EndUTC=datetime(2026, 10, 20, 4),
        PropertyAccessor=accessor,
    )
    after = normalize_appointment(changed, "store")
    accessor.GetProperty.assert_called_once_with(EXCEPTION_REPLACE_TIME)
    assert before["id"] == after["id"]
    assert after["start"] != before["start"]


@pytest.mark.parametrize("status,cancelled", [(0, False), (1, False), (3, False), (5, True), (7, True)])
def test_cancellation_flag(status, cancelled):
    assert normalize_appointment(appointment(MeetingStatus=status), "store")["cancelled"] is cancelled


def test_utc_properties_keep_utc_clock_fields_despite_com_attached_timezone():
    item = appointment(StartUTC=datetime(2026, 10, 20, 1, tzinfo=ZoneInfo("Asia/Tokyo")))
    assert normalize_appointment(item, "store")["start"] == "2026-10-20T01:00:00+00:00"


def test_local_property_fallback_treats_com_date_as_local_clock():
    item = appointment()
    del item.StartUTC
    del item.EndUTC
    item.Start = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    item.End = datetime(2026, 10, 20, 11, tzinfo=timezone.utc)
    result = normalize_appointment(item, "store", ZoneInfo("Asia/Tokyo"))
    assert result["start"] == "2026-10-20T01:00:00+00:00"


def test_location_link_avoids_unnecessary_protected_body_access():
    class Item:
        Location = JOIN

        @property
        def Body(self):
            raise AssertionError("Body was not needed")

        def __getattr__(self, name):
            return getattr(appointment(), name)

    assert normalize_appointment(Item(), "store")["join_url"] == JOIN


def test_guard_denial_is_reported_without_private_com_error_text():
    class Denied:
        Location = ""

        @property
        def Body(self):
            raise RuntimeError("private Outlook body / file / account")

        def __getattr__(self, name):
            return getattr(appointment(), name)

    with pytest.raises(DesktopCalendarError) as error:
        normalize_appointment(Denied(), "store")
    assert "許可" in str(error.value)
    assert "private" not in str(error.value)


def test_html_mapi_fallback_reads_only_html_body():
    accessor = Mock()
    accessor.GetProperty.return_value = f'<a href="{JOIN}">参加</a>'.encode()
    item = appointment(Body="HTML only invitation", PropertyAccessor=accessor)
    assert normalize_appointment(item, "store")["join_url"] == JOIN
    accessor.GetProperty.assert_called_once_with(HTML_BODY)


def test_recurring_enumeration_is_bounded_without_using_count(monkeypatch):
    ns, items = namespace_for([appointment()])
    items.GetNext = lambda: items.appointments[0]
    monkeypatch.setattr("giji.desktop_calendar.MAX_ITEMS", 2)
    with pytest.raises(DesktopCalendarError, match="上限"):
        collect_today(ns, now=TODAY, date_formatter=lambda d: d.isoformat())


def test_process_reader_returns_meetings_and_does_not_pass_auth_settings_to_child():
    meeting = normalize_appointment(appointment(), "store")
    runner = Mock(return_value=subprocess.CompletedProcess(
        [], 0, json.dumps({"ok": True, "meetings": [meeting], "account": "profile"}), "",
    ))
    provider = DesktopOutlookCalendar(runner=runner)
    assert provider.fetch_today({"api_key": "DO NOT PASS THIS", "client_id": ""}) == [meeting]
    args, kwargs = runner.call_args
    assert args[0] == [sys.executable, "-m", "giji.desktop_calendar", "--worker"]
    assert 0 < kwargs["timeout"] <= 8
    assert kwargs["capture_output"] is True
    assert "DO NOT PASS THIS" not in str(runner.call_args)
    assert provider.status()["connected"] is True
    assert provider.status()["account"] == "profile"
    provider.close()
    with pytest.raises(DesktopCalendarError, match="終了"):
        provider.fetch_today({})


def test_frozen_process_uses_explicit_worker_switch(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true,"meetings":[]}', ""))
    DesktopOutlookCalendar(runner=runner).fetch_today({})
    assert runner.call_args.args[0] == [sys.executable, "--calendar-worker"]


def test_hung_outlook_is_timed_out_and_failure_is_visible():
    runner = Mock(side_effect=subprocess.TimeoutExpired(["reader"], 8, output="private data"))
    provider = DesktopOutlookCalendar(runner=runner)
    with pytest.raises(DesktopCalendarError, match="Outlook の応答"):
        provider.fetch_today({})
    assert provider.status()["state"] == "error"
    assert provider.status()["connected"] is False
    assert "private" not in provider.status()["detail"]


@pytest.mark.parametrize("output", ["bad JSON / private data", "[]", '{"ok":true,"meetings":{}}'])
def test_malformed_child_output_is_not_reflected_to_ui(output):
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, output, "private traceback"))
    provider = DesktopOutlookCalendar(runner=runner)
    with pytest.raises(DesktopCalendarError) as error:
        provider.fetch_today({})
    assert "private" not in str(error.value)


def test_worker_outputs_sanitized_json_only(monkeypatch, capsys):
    monkeypatch.setattr("giji.desktop_calendar.read_classic_outlook", Mock(side_effect=RuntimeError("secret details")))
    assert worker_main() == 1
    output = capsys.readouterr()
    assert output.err == ""
    result = json.loads(output.out)
    assert result["ok"] is False
    assert "secret" not in output.out


def test_worker_success_keeps_japanese_usable_without_console_encoding_dependency(monkeypatch, capsys):
    meetings = [normalize_appointment(appointment(), "store")]
    monkeypatch.setattr("giji.desktop_calendar.read_classic_outlook", lambda: (meetings, "プロファイル"))
    assert worker_main() == 0
    output = capsys.readouterr()
    assert output.out.isascii()
    assert json.loads(output.out)["meetings"] == meetings
