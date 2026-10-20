"""Read the signed-in classic Outlook calendar without Graph or app registration.

Outlook Object Model calls execute on the main STA thread of a short-lived
subprocess. A blocked COM call or an Outlook security dialog cannot indefinitely
block the app: the caller terminates only this reader process after its deadline.
No Outlook items, profiles, security settings or messages are modified.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, time as day_time, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .microsoft import valid_teams_url


OL_FOLDER_CALENDAR = 9
OL_APPOINTMENT = 26
OL_RECURRENCE_EXCEPTION = 3
EXCEPTION_REPLACE_TIME = (
    "http://schemas.microsoft.com/mapi/id/"
    "{00062002-0000-0000-C000-000000000046}/82280040"
)
HTML_BODY = "http://schemas.microsoft.com/mapi/proptag/0x10130102"
MAX_ITEMS = 5000
URL_PATTERN = re.compile(r"https://[^\s<>\"'\u3000]+", re.IGNORECASE)


class DesktopCalendarError(RuntimeError):
    """A public error without calendar contents or raw COM exception text."""


def local_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Resolve each local midnight separately, retaining 23/25-hour DST days."""
    if now is None:
        today = datetime.now().date()
        start = datetime.combine(today, day_time.min).astimezone()
        end = datetime.combine(today + timedelta(days=1), day_time.min).astimezone()
    else:
        if now.tzinfo is None:
            raise ValueError("now must include a timezone")
        start = datetime.combine(now.date(), day_time.min, tzinfo=now.tzinfo)
        end = datetime.combine(now.date() + timedelta(days=1), day_time.min, tzinfo=now.tzinfo)
    return start, end


def windows_filter_datetime(value: datetime) -> str:
    """Match Outlook Jet's Windows regional date/time format, without seconds."""
    import win32api

    # pywin32's PyWinObject_AsSYSTEMTIME converts its datetime argument to UTC.
    # These formatting APIs expect wall-clock fields, not an instant. Tag the
    # existing local fields as UTC so the wrapper cannot shift midnight to the
    # previous/next day before handing the SYSTEMTIME to Windows.
    local = value.replace(tzinfo=timezone.utc)
    # LOCALE_USER_DEFAULT, DATE_SHORTDATE and TIME_NOSECONDS. Python's process
    # locale need not equal the user's Windows regional format.
    date = win32api.GetDateFormat(0x0400, 0x00000001, local, None)
    clock = win32api.GetTimeFormat(0x0400, 0x00000002, local, None)
    return f"{date} {clock}"


def extract_teams_url(*values: str) -> str | None:
    """Extract a meeting URL from plain/HTML content, including Safe Links.

    This does not follow redirects or make HTTP requests. Only the embedded
    target of Microsoft's Safe Links is considered, then the Teams allowlist
    and join-path constraints are applied.
    """
    candidates: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for match in URL_PATTERN.finditer(html.unescape(value[:2_000_000])):
            candidate = match.group(0).rstrip(".,;:!?)\\]}）】」』。")
            try:
                parsed = urlsplit(candidate)
                if (
                    parsed.hostname
                    and parsed.hostname.endswith(".safelinks.protection.outlook.com")
                    and parsed.scheme == "https"
                    and not parsed.username
                    and not parsed.password
                    and parsed.port in (None, 443)
                ):
                    candidate = parse_qs(parsed.query).get("url", [""])[0]
                if not valid_teams_url(candidate):
                    continue
                path = urlsplit(candidate).path.lower()
                if path.startswith(("/l/meetup-join/", "/meet/", "/meeting/")):
                    if candidate not in candidates:
                        candidates.append(candidate)
            except ValueError:
                continue
    if not candidates:
        return None
    # Prefer the original long-form meeting link over app-launch/help links.
    return next((url for url in candidates if "/l/meetup-join/" in url.lower()), candidates[0])


def _utc_components(value: Any) -> datetime:
    """COM DATE carries no zone; StartUTC/EndUTC explicitly contain UTC fields.

    pywin32 can attach a tzinfo to a COM DATE. The Outlook property's semantics,
    rather than that attached tzinfo, determine how these fields are interpreted.
    """
    return datetime(
        value.year, value.month, value.day, value.hour, value.minute, value.second,
        getattr(value, "microsecond", 0), tzinfo=timezone.utc,
    )


def _local_components(value: Any, local_zone=None) -> datetime:
    plain = datetime(
        value.year, value.month, value.day, value.hour, value.minute, value.second,
        getattr(value, "microsecond", 0),
    )
    local = plain.replace(tzinfo=local_zone) if local_zone is not None else plain.astimezone()
    return local.astimezone(timezone.utc)


def _appointment_time(item: Any, field: str, local_zone=None) -> datetime:
    try:
        return _utc_components(getattr(item, f"{field}UTC"))
    except (AttributeError, TypeError):
        return _local_components(getattr(item, field), local_zone)


def _optional(item: Any, name: str, default=""):
    try:
        value = getattr(item, name)
        return value if value is not None else default
    except Exception:
        return default


def _join_url(item: Any) -> str | None:
    location = _optional(item, "Location")
    if url := extract_teams_url(location):
        return url
    # Body is protected by Outlook Object Model Guard. If access is denied,
    # report a failed sync, not a misleading successful empty calendar.
    try:
        body = item.Body
    except Exception as exc:
        raise DesktopCalendarError(
            "Outlook が予定本文の読み取りを許可していません。"
            "Outlook の確認画面と組織のポリシーを確認してください。"
        ) from exc
    if url := extract_teams_url(body):
        return url
    rich = _optional(item, "HTMLBody")
    if not rich:
        try:
            rich = item.PropertyAccessor.GetProperty(HTML_BODY)
        except Exception:
            rich = ""
    if isinstance(rich, bytes):
        rich = rich.decode("utf-8", errors="replace")
    return extract_teams_url(rich)


def _occurrence_id(item: Any, store_id: str, start: datetime) -> str:
    base = _optional(item, "GlobalAppointmentID") or _optional(item, "EntryID")
    if not isinstance(base, str) or not base:
        raise DesktopCalendarError("Outlook の予定IDを読み取れませんでした。もう一度同期してください。")
    recurrence = int(_optional(item, "RecurrenceState", 0))
    occurrence = ""
    if recurrence in (2, OL_RECURRENCE_EXCEPTION):
        original = start
        if recurrence == OL_RECURRENCE_EXCEPTION:
            try:
                original = _utc_components(item.PropertyAccessor.GetProperty(EXCEPTION_REPLACE_TIME))
            except Exception as exc:
                raise DesktopCalendarError(
                    "変更された定期会議の元の開催日時を読み取れません。"
                    "Outlook の確認画面を確認して、もう一度同期してください。"
                ) from exc
        occurrence = original.isoformat()
    digest = hashlib.sha256(
        json.dumps([store_id, base, occurrence], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    return f"outlook-{digest}"


def normalize_appointment(item: Any, store_id: str, local_zone=None) -> dict | None:
    if int(_optional(item, "Class", OL_APPOINTMENT)) != OL_APPOINTMENT:
        return None
    start = _appointment_time(item, "Start", local_zone)
    end = _appointment_time(item, "End", local_zone)
    if end <= start:
        return None
    join_url = _join_url(item)
    if not join_url:
        return None
    attendees: list[str] = []
    # Display names only; do not resolve or enumerate address-book recipients.
    for field in ("RequiredAttendees", "OptionalAttendees"):
        for name in str(_optional(item, field)).split(";"):
            name = name.strip()
            if name and name not in attendees:
                attendees.append(name)
    return {
        "id": _occurrence_id(item, store_id, start),
        "title": str(_optional(item, "Subject", "タイトルのない会議")) or "タイトルのない会議",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "join_url": join_url,
        "organizer": str(_optional(item, "Organizer")),
        "attendees": attendees,
        # OlMeetingStatus 5 and 7 both contain the olMeetingCanceled flag.
        "cancelled": bool(int(_optional(item, "MeetingStatus", 0)) & 4),
    }


def collect_today(
    namespace: Any,
    *,
    now: datetime | None = None,
    date_formatter: Callable[[datetime], str] = windows_filter_datetime,
) -> tuple[list[dict], str]:
    """Read the default profile's calendar; safe for fake COM tests."""
    start, end = local_day_bounds(now)
    folder = namespace.GetDefaultFolder(OL_FOLDER_CALENDAR)
    store_id = str(_optional(folder, "StoreID"))
    items = folder.Items
    items.Sort("[Start]", False)
    items.IncludeRecurrences = True
    # Jet interprets these named properties and date values in local time.
    lower = date_formatter(start).replace("'", "''")
    upper = date_formatter(end).replace("'", "''")
    restricted = items.Restrict(f"[Start] < '{upper}' AND [End] > '{lower}'")
    # Count is undefined for expanded recurring collections. Never read it.
    item = restricted.GetFirst()
    results: dict[str, dict] = {}
    count = 0
    while item is not None:
        count += 1
        if count > MAX_ITEMS:
            raise DesktopCalendarError(
                "当日の予定の件数が上限を超えました。Outlook の定期予定を確認してください。"
            )
        meeting = normalize_appointment(item, store_id, now.tzinfo if now else None)
        if meeting:
            begins = datetime.fromisoformat(meeting["start"])
            ends = datetime.fromisoformat(meeting["end"])
            # Defend against unusual provider filtering results and boundary events.
            if begins < end and ends > start:
                results[meeting["id"]] = meeting
        item = restricted.GetNext()
    account = str(_optional(namespace, "CurrentProfileName"))[:200]
    return sorted(results.values(), key=lambda meeting: meeting["start"]), account


def read_classic_outlook() -> tuple[list[dict], str]:
    """Called only on the worker process's main STA thread."""
    if sys.platform != "win32":
        raise DesktopCalendarError("Outlook のデスクトップ連携は Windows 11 で利用できます。")
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise DesktopCalendarError("Outlook 連携ライブラリがありません。セットアップを実行してください。") from exc
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    application = namespace = None
    try:
        try:
            application = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            # Start installed classic Outlook with its existing/default profile.
            # Never create a profile, call Logon, change policy or close Outlook.
            application = win32com.client.Dispatch("Outlook.Application")
        namespace = application.GetNamespace("MAPI")
        return collect_today(namespace)
    except DesktopCalendarError:
        raise
    except Exception as exc:
        raise DesktopCalendarError(
            "クラシック Outlook の予定を読み取れません。Outlook を開いてログインし、"
            "既定の予定表が表示できることを確認してください。新しい Outlook は対象外です。"
        ) from exc
    finally:
        namespace = application = None
        pythoncom.CoUninitialize()


def worker_main() -> int:
    """The worker pipe contains normalized data or a sanitized error only."""
    try:
        meetings, account = read_classic_outlook()
        result = {"ok": True, "meetings": meetings, "account": account}
        code = 0
    except DesktopCalendarError as exc:
        result = {"ok": False, "error": str(exc)}
        code = 1
    except Exception:
        result = {"ok": False, "error": "Outlook の予定を読み取れませんでした。Outlook の状態を確認してください。"}
        code = 1
    # JSON ASCII escapes avoid Windows console codepage differences.
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.flush()
    return code


class DesktopOutlookCalendar:
    """Bounded, read-only calendar provider using the user's Outlook profile."""

    def __init__(self, timeout_seconds: float = 8, runner=None) -> None:
        self._timeout = max(0.1, timeout_seconds)
        self._runner = runner or subprocess.run
        self._native = runner is None
        self._lock = threading.Lock()
        self._closed = False
        self._status = {
            "state": "unknown", "connected": False, "provider": "outlook_desktop",
            "detail": "クラシック Outlook の予定表を確認しています。", "account": "",
        }

    def status(self, settings: dict | None = None) -> dict:
        if self._native and sys.platform != "win32":
            return {
                **self._status, "state": "unavailable", "connected": False,
                "detail": "Outlook のデスクトップ連携は Windows 11 で利用できます。",
            }
        return dict(self._status)

    def is_connected(self) -> bool:
        return bool(self.status()["connected"])

    def fetch_today(self, settings: dict | None = None) -> list[dict]:
        if self._closed:
            raise DesktopCalendarError("Outlook の監視を終了しました。")
        if self._native and sys.platform != "win32":
            raise DesktopCalendarError(self.status()["detail"])
        began = time.monotonic()
        if not self._lock.acquire(timeout=self._timeout):
            raise DesktopCalendarError("Outlook の同期を処理中です。")
        try:
            command = (
                [sys.executable, "--calendar-worker"]
                if getattr(sys, "frozen", False)
                else [sys.executable, "-m", "giji.desktop_calendar", "--worker"]
            )
            self._status.update(state="syncing", detail="クラシック Outlook の予定を読み取り中です。")
            try:
                process = self._runner(
                    command, capture_output=True, text=True, encoding="utf-8",
                    timeout=max(0.1, self._timeout - (time.monotonic() - began)),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise DesktopCalendarError(
                    "Outlook の応答を待っています。Outlook を開き、"
                    "ログイン・プロファイル選択・読み取り確認の画面が出ていないか確認してください。"
                ) from exc
            except Exception as exc:
                raise DesktopCalendarError("Outlook の読み取り処理を開始できませんでした。") from exc
            try:
                payload = json.loads(process.stdout)
                if not isinstance(payload, dict):
                    raise ValueError("worker format")
            except (TypeError, ValueError) as exc:
                raise DesktopCalendarError("Outlook の読み取り結果を確認できませんでした。") from exc
            if not payload.get("ok") or process.returncode:
                error = payload.get("error")
                raise DesktopCalendarError(
                    error if isinstance(error, str) and len(error) <= 500
                    else "Outlook の予定を読み取れませんでした。"
                )
            meetings = payload.get("meetings")
            if not isinstance(meetings, list):
                raise DesktopCalendarError("Outlook の読み取り結果が正しくありません。")
            self._status.update(
                state="ready", connected=True, detail="クラシック Outlook と同期しました。",
                account=str(payload.get("account", ""))[:200],
            )
            return meetings
        except DesktopCalendarError as exc:
            self._status.update(state="error", connected=False, detail=str(exc))
            raise
        finally:
            self._lock.release()

    def close(self) -> None:
        self._closed = True
        self._status.update(state="unavailable", connected=False, detail="Outlook の監視を終了しました。")


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("Use the giji application to read the Outlook calendar.")
    raise SystemExit(worker_main())
