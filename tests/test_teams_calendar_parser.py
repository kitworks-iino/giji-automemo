"""Synthetic DOM/AX observations, not fixtures captured from Microsoft Teams."""

from datetime import datetime, timezone

import pytest

from giji.teams_calendar_parser import CalendarParseError, parse_calendar_snapshot

NOW = datetime(2026, 10, 21, 3, tzinfo=timezone.utc)


def snapshot(*events, **overrides):
    result = {
        "url": "https://teams.microsoft.com/v2/",
        "timezone": "Asia/Tokyo",
        "date_texts": ["2026年10月21日"],
        "day_view": True,
        "empty_text": "",
        "events": list(events),
    }
    result.update(overrides)
    return result


def test_japanese_label_gets_utc_times_and_visible_coverage():
    result = parse_calendar_snapshot(snapshot({"label": "定例会議, 2026年10月21日, 10:00から11:00"}), NOW)
    event = result["meetings"][0]
    assert event["title"] == "定例会議"
    assert event["start"] == "2026-10-21T01:00:00+00:00"
    assert event["end"] == "2026-10-21T02:00:00+00:00"
    assert event["join_url"] == ""
    assert result["coverage"] == "visible_only"
    assert result["confirmed_today"] is True
    assert any("タイムゾーン" in warning for warning in result["warnings"])


def test_english_label_reads_name_and_meridiem():
    result = parse_calendar_snapshot(snapshot({
        "label": "Design review, Wednesday, October 21, 2026, 10:00 AM to 11:00 AM",
    }), NOW)
    event = result["meetings"][0]
    assert event["title"] == "Design review"
    assert event["start"] == "2026-10-21T01:00:00+00:00"


def test_offset_iso_does_not_reinterpret_in_browser_timezone():
    result = parse_calendar_snapshot(snapshot({
        "key": "observed-event-key", "title": "Offset meeting",
        "start": "2026-10-21T10:00:00+02:00", "end": "2026-10-21T11:00:00+02:00",
        "join_url": "https://teams.microsoft.com/meet/123?p=abc",
    }), NOW)
    assert result["meetings"][0]["start"] == "2026-10-21T08:00:00+00:00"
    assert len(result["warnings"]) == 1


def test_naive_iso_requires_observed_timezone():
    event = {"title": "Review", "start": "2026-10-21T10:00:00", "end": "2026-10-21T11:00:00"}
    result = parse_calendar_snapshot(snapshot(event), NOW)
    assert result["meetings"][0]["start"] == "2026-10-21T01:00:00+00:00"
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot(event, timezone=""), NOW)


def test_today_button_alone_does_not_supply_date():
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot({"label": "Daily review, 10:00 to 11:00"}, date_texts=["Today", "今日"]), NOW)


def test_time_only_needs_single_explicit_today_and_day_view():
    event = {"title": "Daily", "label": "10:00から11:00"}
    assert parse_calendar_snapshot(snapshot(event), NOW)["meetings"]
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot(event, day_view=False), NOW)
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot(event, date_texts=["2026年10月21日", "2026年10月22日"]), NOW)


def test_partial_date_needs_an_observed_year_not_current_year():
    event = {"label": "Review, October 21, 10:00 AM to 11:00 AM"}
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot(event, date_texts=["October 21"], day_view=False), NOW)
    result = parse_calendar_snapshot(snapshot(event, date_texts=["October 2026", "October 21"], day_view=False), NOW)
    assert result["meetings"][0]["start"] == "2026-10-21T01:00:00+00:00"


def test_japanese_monthday_uses_year_header():
    result = parse_calendar_snapshot(snapshot(
        {"label": "Review, 10月21日, 午後1:00から午後2:00"},
        date_texts=["2026年10月", "10月21日"],
    ), NOW)
    assert result["meetings"][0]["start"] == "2026-10-21T04:00:00+00:00"


def test_missing_end_skipped_but_other_events_survive():
    result = parse_calendar_snapshot(snapshot(
        {"title": "Valid", "label": "10:00から11:00"},
        {"title": "Unknown end", "label": "12:00"},
    ), NOW)
    assert len(result["meetings"]) == 1
    assert any("1 件" in warning for warning in result["warnings"])


@pytest.mark.parametrize("empty_text", ["", "Loading", "読み込み中", "No results"])
def test_zero_candidates_are_never_silently_successful(empty_text):
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot(empty_text=empty_text), NOW)


@pytest.mark.parametrize("empty_text", ["No events today", "今日の予定はありません"])
def test_explicit_empty_day_is_complete_only_with_confirmed_date(empty_text):
    assert parse_calendar_snapshot(snapshot(empty_text=empty_text), NOW) == {
        "meetings": [], "coverage": "complete", "warnings": [], "confirmed_today": True,
    }
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot(empty_text=empty_text, date_texts=["Today"]), NOW)


def test_unparseable_candidate_prevents_empty_success_even_with_empty_text():
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot({"label": "Unrecognized calendar card"}, empty_text="No events today"), NOW)


def test_ids_distinguish_recurring_occurrences_and_deduplicate_same_observation():
    event = {"key": "series-key", "title": "Daily", "label": "10:00から11:00"}
    another = {**event, "label": "13:00から14:00"}
    result = parse_calendar_snapshot(snapshot(event, event, another), NOW)
    assert len(result["meetings"]) == 2
    assert len({item["id"] for item in result["meetings"]}) == 2
    assert all(item["id"].startswith("teams-") for item in result["meetings"])


def test_unsafe_links_are_discarded_without_losing_the_event():
    result = parse_calendar_snapshot(snapshot({
        "title": "Daily", "label": "10:00から11:00",
        "join_url": "https://teams.microsoft.com.attacker.example/meet/123",
    }), NOW)
    assert result["meetings"][0]["join_url"] == ""
    assert any("正規URL" in warning for warning in result["warnings"])


def test_cancelled_prefix_is_recognized_but_cancellation_topic_is_not():
    cancelled = {"label": "Canceled: Daily, 2026-10-21, 10:00 to 11:00"}
    normal = {"label": "Cancellation policy review, 2026-10-21, 13:00 to 14:00"}
    result = parse_calendar_snapshot(snapshot(cancelled, normal), NOW)
    assert [item["cancelled"] for item in result["meetings"]] == [True, False]


def test_overnight_iso_overlapping_today_is_kept_and_previous_day_is_skipped():
    crossing = {"title": "Overnight", "start": "2026-10-20T23:30:00+09:00", "end": "2026-10-21T00:30:00+09:00"}
    old = {"title": "Yesterday", "start": "2026-10-20T10:00:00+09:00", "end": "2026-10-20T11:00:00+09:00"}
    result = parse_calendar_snapshot(snapshot(crossing, old), NOW)
    assert [item["title"] for item in result["meetings"]] == ["Overnight"]


def test_dst_ambiguous_label_requires_explicit_offset():
    now = datetime(2026, 11, 1, 7, tzinfo=timezone.utc)
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot(
            {"title": "Fall back", "label": "01:00 to 01:30"},
            timezone="America/New_York", date_texts=["2026-11-01"],
        ), now)


def test_invalid_observed_iso_is_not_replaced_with_plausible_label_times():
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot({
            "label": "10:00から11:00", "start": "2026-02-30T10:00:00", "end": "2026-10-21T11:00:00",
        }), NOW)


@pytest.mark.parametrize("event", [
    {"label": "Review, 2026-02-30, 10:00から11:00"},
    {"label": "Review, 10:00から11:00", "date": "invalid-date"},
])
def test_invalid_observed_date_is_never_replaced_with_today(event):
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot(event), NOW)


def test_ambiguous_shared_meridiem_does_not_invent_overnight_meeting():
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot({"label": "Review, 11:00 to 1:00 PM"}), NOW)


@pytest.mark.parametrize(("month", "day"), [
    ("2026年10月", "21(水)"),
    ("October 2026", "21 Wed"),
])
def test_split_month_and_day_require_observed_day_column(month, day):
    event = {"label": "Daily, 10:00 to 11:00"}
    result = parse_calendar_snapshot(snapshot(
        event, date_texts=[month, day], day_column_texts=[day],
    ), NOW)
    assert result["meetings"][0]["start"] == "2026-10-21T01:00:00+00:00"
    # A standalone heading/mini-calendar number has no column provenance.
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot(event, date_texts=[month, day]), NOW)


@pytest.mark.parametrize("overrides", [
    {"day_view": False},
    {"day_column_texts": ["21(水)", "22(木)"]},
    {"date_texts": ["2026年10月", "2026年11月"]},
    {"date_texts": ["2026年10月", "2026年10月22日"]},
    {"date_texts": ["10月"]},
    {"day_column_texts": ["32(水)"]},
])
def test_split_headers_never_guess_weekview_year_or_ambiguous_column(overrides):
    options = {"date_texts": ["2026年10月"], "day_column_texts": ["21(水)"]}
    options.update(overrides)
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(snapshot({"label": "Daily, 10:00 to 11:00"}, **options), NOW)


@pytest.mark.parametrize(("label", "expected"), [
    ("10:00 AM to 11:00 AM, Design review", "Design review"),
    ("10:00 AM to 11:00 AM, Wednesday, October 21, 2026, Design review", "Design review"),
    ("10:00から11:00、定例会議、2026年10月21日", "定例会議"),
    ("2026年10月21日、定例会議、10:00から11:00", "定例会議"),
    ("10:00から11:00、2026年10月21日", "タイトルを取得できない会議"),
])
def test_time_first_label_preserves_observed_subject(label, expected):
    result = parse_calendar_snapshot(snapshot({"label": label}), NOW)
    assert result["meetings"][0]["title"] == expected


def test_keyless_duration_update_has_stable_identity():
    first = parse_calendar_snapshot(snapshot({"title": "Daily", "label": "10:00 to 11:00"}), NOW)
    extended = parse_calendar_snapshot(snapshot({"title": "Daily", "label": "10:00 to 11:30"}), NOW)
    assert first["meetings"][0]["id"] == extended["meetings"][0]["id"]
    assert first["meetings"][0]["end"] != extended["meetings"][0]["end"]
