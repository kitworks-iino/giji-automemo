from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from giji.microsoft import (
    CACHE_KEY,
    CalendarError,
    MicrosoftCalendar,
    normalize_meeting,
    today_range,
    valid_teams_url,
)


class MemorySecrets:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


def event(event_id="e1", **overrides):
    result = {
        "id": event_id,
        "subject": "定例会議",
        "start": {"dateTime": "2026-10-20T01:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-20T02:00:00.0000000", "timeZone": "UTC"},
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/19%3aexample"},
        "organizer": {"emailAddress": {"name": "主催者", "address": "owner@example.com"}},
        "attendees": [{"emailAddress": {"name": "田中"}}],
        "isCancelled": False,
    }
    result.update(overrides)
    return result


def test_today_range_handles_dst_short_and_long_days():
    zone = ZoneInfo("America/New_York")
    start, end = today_range(datetime(2026, 3, 8, 12, tzinfo=zone))
    assert (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() == 23 * 3600
    start, end = today_range(datetime(2026, 11, 1, 12, tzinfo=zone))
    assert (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() == 25 * 3600


def test_today_range_uses_local_calendar_date():
    start, end = today_range(datetime(2026, 10, 20, 1, tzinfo=ZoneInfo("Asia/Tokyo")))
    assert start == "2026-10-19T15:00:00+00:00"
    assert end == "2026-10-20T15:00:00+00:00"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://teams.microsoft.com.evil.example/l/meeting",
        "https://teams.microsoft.com@evil.example/l/meeting",
        "http://teams.microsoft.com/l/meeting",
        "https://teams.microsoft.com:8443/l/meeting",
        "https://teams.microsoft.com:invalid/l/meeting",
    ],
)
def test_join_url_rejects_untrusted_targets(url):
    assert not valid_teams_url(url)


def test_event_normalizes_utc_and_preserves_cancelled_status():
    meeting = normalize_meeting(event(isCancelled=True))
    assert meeting["cancelled"] is True
    assert meeting["start"] == "2026-10-20T01:00:00+00:00"
    assert meeting["attendees"] == ["田中"]
    assert normalize_meeting(event(onlineMeeting=None)) is None


def test_event_refuses_to_guess_unexpected_timezone():
    with pytest.raises(ValueError):
        normalize_meeting(
            event(start={"dateTime": "2026-10-20T10:00:00", "timeZone": "Tokyo Standard Time"})
        )


def install_graph_transport(monkeypatch, handler):
    original_client = httpx.Client
    monkeypatch.setattr(
        "giji.microsoft.httpx.Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )


def connected_calendar(monkeypatch):
    calendar = MicrosoftCalendar(MemorySecrets())
    monkeypatch.setattr(calendar, "_access_token", lambda settings: "test-token")
    return calendar


def test_calendar_paginates_and_deduplicates_occurrences(monkeypatch):
    calendar = connected_calendar(monkeypatch)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if len(requests) == 1:
            assert "startDateTime" in request.url.params
            return httpx.Response(
                200,
                json={
                    "value": [event("one")],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView?$skiptoken=next",
                },
            )
        assert dict(request.url.params) == {"$skiptoken": "next"}
        return httpx.Response(200, json={"value": [event("one"), event("two", isCancelled=True)]})

    install_graph_transport(monkeypatch, handle)
    result = calendar.fetch_today({})
    assert [meeting["id"] for meeting in result] == ["one", "two"]
    assert result[1]["cancelled"]
    assert len(requests) == 2


def test_calendar_does_not_leak_token_to_malicious_next_page(monkeypatch):
    calendar = connected_calendar(monkeypatch)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"value": [event()], "@odata.nextLink": "https://example.com/collect-token"},
        )

    install_graph_transport(monkeypatch, handle)
    with pytest.raises(CalendarError, match="ページ情報"):
        calendar.fetch_today({})
    assert len(requests) == 1


def test_graph_errors_do_not_include_response_body_or_tokens(monkeypatch):
    calendar = connected_calendar(monkeypatch)
    install_graph_transport(
        monkeypatch, lambda request: httpx.Response(403, text="secret-token-sensitive-details")
    )
    with pytest.raises(CalendarError) as error:
        calendar.fetch_today({})
    assert "Calendars.ReadBasic" in str(error.value)
    assert "secret-token" not in str(error.value)


def test_cancelled_device_login_cannot_restore_logged_out_credentials():
    secrets = MemorySecrets()
    calendar = MicrosoftCalendar(secrets)
    flow = {"expires_at": 100, "device_code": "private"}
    calendar._flow = flow

    def finish_then_logout(current_flow):
        calendar.logout()
        return {"access_token": "must-not-save"}

    calendar._app = SimpleNamespace(acquire_token_by_device_flow=finish_then_logout)
    assert calendar.complete_login() is False
    assert flow["expires_at"] == 0
    assert secrets.get(CACHE_KEY) is None


def test_configuration_rejects_authority_path_injection():
    with pytest.raises(CalendarError, match="テナントID"):
        MicrosoftCalendar._configuration(
            {"client_id": "00000000-0000-0000-0000-000000000001", "tenant_id": "../evil"}
        )
