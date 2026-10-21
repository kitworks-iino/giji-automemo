"""Conservative parsing of visible Teams calendar DOM/AX snapshots.

This module consumes observations, not a supported Microsoft calendar API. Its
tests use synthetic observations; they are not captures of a real Teams build.
No missing time, year, identifier, or meeting URL is fetched or invented here.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CalendarParseError(RuntimeError):
    """Safe error indicating that the current view cannot replace prior data."""


_MONTHS = {
    name.lower(): index
    for index, names in enumerate(
        (
            ("January", "Jan"), ("February", "Feb"), ("March", "Mar"),
            ("April", "Apr"), ("May",), ("June", "Jun"), ("July", "Jul"),
            ("August", "Aug"), ("September", "Sep", "Sept"),
            ("October", "Oct"), ("November", "Nov"), ("December", "Dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_FULL_DATES = (
    re.compile(r"(?<!\d)(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"),
    re.compile(r"(?<!\d)(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})(?!\d)"),
    re.compile(rf"\b(?P<month_name>{_MONTH})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s+(?P<year>\d{{4}})\b", re.I),
    re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month_name>{_MONTH})\s*,?\s+(?P<year>\d{{4}})\b", re.I),
)
_PARTIAL_DATES = (
    re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"),
    re.compile(rf"\b(?P<month_name>{_MONTH})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b", re.I),
    re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month_name>{_MONTH})\b", re.I),
)
_TIME = re.compile(
    r"(?<![\d:])(?:(?P<prefix>午前|午後|am|pm)\s*)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2})|時(?P<jp_minute>\d{1,2})?分?)"
    r"\s*(?P<suffix>am|pm)?(?![\d:])", re.I,
)
_CONNECTOR = re.compile(r"\s*(?:to|until|から|~|〜|–|—|-|→)\s*", re.I)
_WEEKDAY = re.compile(
    r"(?:\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b[.,]?|[月火水木金土日]曜日)\s*[,、]?\s*$",
    re.I,
)
_WEEKDAY_PREFIX = re.compile(
    r"^(?:\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b[.,]?|[月火水木金土日]曜日)\s*[,、]?\s*",
    re.I,
)
_MONTH_HEADER = (
    re.compile(r"(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月"),
    re.compile(rf"(?P<month_name>{_MONTH})\s+(?P<year>\d{{4}})", re.I),
)
_DAY_COLUMN = re.compile(
    r"(?P<day>\d{1,2})(?:日)?(?:\s*\(?\s*(?:[月火水木金土日](?:曜日)?|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s*\)?)?",
    re.I,
)
_EMPTY = re.compile(
    r"(?:no\s+(?:events|meetings)(?:\s+(?:scheduled|today|for today))?"
    r"|there\s+are\s+no\s+(?:events|meetings)(?:\s+(?:scheduled|today|for today))?"
    r"|nothing\s+scheduled(?:\s+(?:today|for today))?"
    r"|(?:本日|今日)の(?:予定|イベント|会議)はありません"
    r"|(?:今日|本日)は(?:予定|イベント|会議)がありません"
    r"|(?:予定|イベント|会議)(?:は|が)ありません)[.!。]?", re.I,
)
_CANCELLED = re.compile(
    r"^(?:(?:canceled|cancelled|中止|キャンセル済み|キャンセル)\s*[:：]"
    r"|[\[(【](?:canceled|cancelled|中止|キャンセル済み|キャンセル)[\])】])", re.I,
)
_PARTIAL_WARNING = "画面で確認できた予定のみです。画面外・非表示の予定は含まれない場合があります。"
_TIMEZONE_WARNING = "時差のない表示時刻は、Teams画面から取得したブラウザーのタイムゾーンで解釈しました。"


def _text(value: Any, limit: int = 8000) -> str:
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value[:limit]).strip()


def _dates(text: str, observed_year: int | None = None) -> list[tuple[date, int, int]]:
    """Find explicit dates, allowing a missing year only from observed headers."""
    result: list[tuple[date, int, int]] = []
    occupied: list[tuple[int, int]] = []
    patterns = _FULL_DATES + (_PARTIAL_DATES if observed_year is not None else ())
    for pattern in patterns:
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            # Mark invalid dates too, so e.g. 2026-02-30 cannot become a partial date.
            occupied.append(match.span())
            fields = match.groupdict()
            try:
                year = int(fields.get("year") or observed_year)
                month = (
                    _MONTHS[fields["month_name"].lower().rstrip(".")]
                    if fields.get("month_name") else int(fields["month"])
                )
                found = date(year, month, int(fields["day"]))
            except (TypeError, ValueError, KeyError):
                continue
            result.append((found, match.start(), match.end()))
    return sorted(result, key=lambda value: value[1])


def _observed_year(texts: list[str]) -> int | None:
    years = {
        int(match.group()) for text in texts
        for match in re.finditer(r"(?<!\d)(?:19|20|21)\d{2}(?!\d)", text)
    }
    return next(iter(years)) if len(years) == 1 else None


def _split_day_header(texts: list[str], columns: list[str]) -> date | None:
    """Combine a month header only with one observed day-column number.

    The collector supplies column names separately from headings: arbitrary
    page text, current-date markers and date-picker numbers are not evidence
    of the displayed day. The caller also requires an observed day view.
    """
    months = set()
    for text in texts:
        for pattern in _MONTH_HEADER:
            match = pattern.fullmatch(text)
            if match:
                fields = match.groupdict()
                month = (
                    _MONTHS[fields["month_name"].lower().rstrip(".")]
                    if fields.get("month_name") else int(fields["month"])
                )
                months.add((int(fields["year"]), month))
    days = {
        int(match["day"]) for column in columns
        if (match := _DAY_COLUMN.fullmatch(column)) is not None
    }
    if len(months) != 1 or len(days) != 1:
        return None
    try:
        return date(*next(iter(months)), next(iter(days)))
    except ValueError:
        return None


def _localize(value: datetime, zone: ZoneInfo) -> datetime:
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)
    # An omitted UTC offset cannot resolve repeated or nonexistent wall times.
    if first.utcoffset() != second.utcoffset():
        raise ValueError("ambiguous local time")
    if first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != value:
        raise ValueError("nonexistent local time")
    return first


def _iso(value: Any, zone: ZoneInfo | None) -> tuple[datetime, bool] | None:
    raw = _text(value, 100)
    if not raw or not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", raw):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed, False
        if zone is not None:
            return _localize(parsed, zone), True
    except ValueError:
        pass
    return None


def _meridiem(match: re.Match[str]) -> str | None:
    prefix, suffix = match.group("prefix"), match.group("suffix")

    def normalize(value: str) -> str:
        return {"午前": "am", "午後": "pm"}.get(value, value.lower())

    if prefix and suffix and normalize(prefix) != normalize(suffix):
        raise ValueError("conflicting meridiem")
    return normalize(prefix or suffix) if prefix or suffix else None


def _clock(match: re.Match[str], meridiem: str | None) -> time:
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or match.group("jp_minute") or 0)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError("invalid meridiem hour")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    return time(hour, minute)


def _time_range(text: str) -> tuple[time, time, int, int] | None:
    matches = list(_TIME.finditer(text))
    candidates = []
    for first, second in zip(matches, matches[1:]):
        if not _CONNECTOR.fullmatch(text[first.end():second.start()]):
            continue
        try:
            left, right = _meridiem(first), _meridiem(second)
            # A shared AM/PM is acceptable only when its interpretation yields
            # a forward interval. An ambiguous "11 to 1 PM" is rejected below.
            start = _clock(first, left or right)
            end = _clock(second, right or left)
            if end <= start:
                continue
            candidates.append((start, end, first.start(), second.end()))
        except ValueError:
            continue
    return candidates[0] if len(candidates) == 1 else None


def _join_url(value: Any) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft"}
            and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
            and parsed.path.strip("/")
        ):
            return value
    except ValueError:
        pass
    return ""


def _title(event: dict[str, Any], label: str, observed_year: int | None) -> str:
    explicit = _text(event.get("title"), 500)
    if explicit:
        return explicit
    boundaries = [start for _, start, _ in _dates(label, observed_year)]
    boundaries.extend(match.start() for match in _TIME.finditer(label))
    prefix = label[:min(boundaries)] if boundaries else label
    prefix = _WEEKDAY.sub("", prefix.strip(" ,、\n"))
    prefix = prefix.strip(" ,、;\n-–")
    if prefix:
        return prefix[:500]
    # Some accessible labels begin with the time interval, followed by the
    # subject. Strip only complete date/weekday metadata at the boundaries;
    # never invent a subject from a date, time, or accessibility role.
    interval = _time_range(label)
    if interval:
        for part in (label[:interval[2]], label[interval[3]:]):
            for _ in range(8):
                previous = part
                part = part.strip(" ,、;:\n-–—()[]")
                part = _WEEKDAY_PREFIX.sub("", _WEEKDAY.sub("", part))
                dates = _dates(part, observed_year)
                if dates and dates[0][1] == 0:
                    part = part[dates[0][2]:]
                elif dates and dates[-1][2] == len(part):
                    part = part[:dates[-1][1]]
                if part == previous:
                    break
            if part.strip(" ,、;:\n-–—()[]"):
                return part.strip(" ,、;:\n-–—()[]")[:500]
    return "タイトルを取得できない会議"


def _event_times(
    event: dict[str, Any], zone: ZoneInfo | None, observed_year: int | None,
    today: date | None, time_only_allowed: bool,
) -> tuple[datetime, datetime, bool] | None:
    start_attr, end_attr = _text(event.get("start")), _text(event.get("end"))
    if start_attr or end_attr:
        start, end = _iso(start_attr, zone), _iso(end_attr, zone)
        # Do not silently replace a malformed/missing observed attribute with
        # a guessed time from another part of the page.
        if start is None or end is None:
            return None
        if end[0].astimezone(timezone.utc) <= start[0].astimezone(timezone.utc):
            return None
        return start[0], end[0], start[1] or end[1]
    if zone is None:
        return None
    label = _text(event.get("label")) or _text(event.get("text"))
    date_attr = _text(event.get("date"), 100)
    dates = _dates(date_attr, observed_year) if date_attr else _dates(label, observed_year)
    unique_dates = {value for value, _, _ in dates}
    if len(unique_dates) == 1:
        event_date = next(iter(unique_dates))
    elif (
        not unique_dates and not date_attr
        and not any(pattern.search(label) for pattern in _FULL_DATES + _PARTIAL_DATES)
        and time_only_allowed and today is not None
    ):
        event_date = today
    else:
        return None
    times = _time_range(label)
    if times is None:
        return None
    try:
        start = _localize(datetime.combine(event_date, times[0]), zone)
        end = _localize(datetime.combine(event_date, times[1]), zone)
    except ValueError:
        return None
    return start, end, True


def parse_calendar_snapshot(snapshot: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Parse only dated, bounded events overlapping the observed local today.

    A successful visible-only result is a merge, not evidence that missing
    events were cancelled. Only an independently dated, explicit empty day
    view can produce an empty result with complete coverage.
    """
    if not isinstance(snapshot, dict):
        raise CalendarParseError("Teams予定表の画面情報を読み取れませんでした。")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise CalendarParseError("現在時刻のタイムゾーンを確認できませんでした。")
    raw_zone = _text(snapshot.get("timezone"), 100)
    try:
        zone = ZoneInfo(raw_zone) if raw_zone else None
    except (ValueError, ZoneInfoNotFoundError):
        zone = None
    today = now.astimezone(zone).date() if zone is not None else None
    raw_texts = snapshot.get("date_texts")
    texts = [_text(value, 500) for value in raw_texts[:200]] if isinstance(raw_texts, list) else []
    year = _observed_year(texts)
    header_dates = {value for text in texts for value, _, _ in _dates(text, year)}
    day_view = snapshot.get("day_view") is True
    raw_columns = snapshot.get("day_column_texts")
    columns = [_text(value, 500) for value in raw_columns[:200]] if isinstance(raw_columns, list) else []
    if day_view and (split_date := _split_day_header(texts, columns)) is not None:
        header_dates.add(split_date)
    headers_confirm_today = today is not None and today in header_dates
    time_only_allowed = day_view and headers_confirm_today and header_dates == {today}
    raw_events = snapshot.get("events")
    if not isinstance(raw_events, list):
        raise CalendarParseError("Teams予定表の予定一覧を確認できませんでした。")
    meetings: dict[str, dict[str, Any]] = {}
    warnings = [_PARTIAL_WARNING]
    skipped = 0
    used_local_time = False
    invalid_link = False
    for event in raw_events[:5000]:
        if not isinstance(event, dict):
            skipped += 1
            continue
        parsed = _event_times(event, zone, year, today, time_only_allowed)
        if parsed is None:
            skipped += 1
            continue
        start, end, assumed_zone = parsed
        # Explicit offsets remain usable when Intl timezone information is
        # unavailable; their own offset supplies the local date boundary.
        event_zone = zone or start.tzinfo
        event_today = now.astimezone(event_zone).date()
        day_start = datetime.combine(event_today, time.min, tzinfo=event_zone)
        day_end = datetime.combine(event_today + timedelta(days=1), time.min, tzinfo=event_zone)
        if start >= day_end or end <= day_start:
            continue
        label = _text(event.get("label")) or _text(event.get("text"))
        title = _title(event, label, year)
        start_iso = start.astimezone(timezone.utc).isoformat()
        end_iso = end.astimezone(timezone.utc).isoformat()
        key = _text(event.get("key"), 2000)
        # Duration edits must update the same visible occurrence. With no
        # observed key, simultaneous identically named meetings cannot be
        # distinguished safely; never fabricate an occurrence identifier.
        identity = ["observed", key, start_iso] if key else ["visible", title, start_iso]
        event_id = "teams-" + hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()[:32]
        join_url = _join_url(event.get("join_url"))
        invalid_link = invalid_link or bool(event.get("join_url") and not join_url)
        meetings[event_id] = {
            "id": event_id, "title": title, "start": start_iso, "end": end_iso,
            "join_url": join_url, "organizer": "", "attendees": [],
            "cancelled": bool(_CANCELLED.match(label) or _CANCELLED.match(title)),
        }
        used_local_time = used_local_time or assumed_zone
    if len(raw_events) > 5000:
        skipped += len(raw_events) - 5000
    if not meetings:
        empty = _EMPTY.fullmatch(_text(snapshot.get("empty_text"), 500)) is not None
        if empty and time_only_allowed and not raw_events:
            return {"meetings": [], "coverage": "complete", "warnings": [], "confirmed_today": True}
        raise CalendarParseError(
            "今日の会議日時を画面から確認できませんでした。Teamsで今日の予定表を表示してください。保存済みの予定は保持されます。"
        )
    if used_local_time:
        warnings.append(_TIMEZONE_WARNING)
    if skipped:
        warnings.append(f"日時を確定できない予定候補 {skipped} 件は取り込みませんでした。")
    if invalid_link:
        warnings.append("Teamsの正規URLと確認できない参加リンクは取り込みませんでした。")
    if zone is None:
        warnings.append("画面のタイムゾーンを取得できないため、時差が明記された予定だけを取り込みました。")
    return {
        "meetings": sorted(meetings.values(), key=lambda event: (event["start"], event["id"])),
        "coverage": "visible_only", "warnings": warnings, "confirmed_today": True,
    }
