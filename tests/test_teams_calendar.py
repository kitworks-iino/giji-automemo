"""Transport/reader tests use synthetic UI observations, never real Teams data."""

import json
import io
import struct
import time
from collections import deque
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest

from giji.teams_calendar import (
    CDPClient, TeamsCalendarError, TeamsDesktopCalendar, _CalendarReader,
    _SNAPSHOT_JS, _allowed_origin, _bounded_socket_class,
)


ENDPOINT = "http://127.0.0.1:9222"
SOCKET_URL = "ws://127.0.0.1:9222/devtools/browser/test-browser"


def snapshot(**changes):
    today = datetime.now(timezone.utc).date().isoformat()
    result = {
        "url": "https://outlook.office.com/hosted/calendar/", "timezone": "UTC",
        "calendar_present": True, "date_texts": [today], "day_view": True,
        "empty_text": "", "events": [{"key": "event-1", "title": "Synthetic review",
                                      "start": f"{today}T10:00:00Z", "end": f"{today}T11:00:00Z"}],
    }
    result.update(changes)
    return result


class FakeSocket:
    def __init__(self, responder):
        self.queue = deque()
        self.responder = responder
        self.sent = []
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        self.queue.extend(self.responder(request))

    def recv(self):
        return json.dumps(self.queue.popleft())

    def abort(self):
        self.closed = True

    def close(self, **kwargs):
        self.closed = True


def transport(socket, metadata=None, status_code=200):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        status_code, json=metadata or {"webSocketDebuggerUrl": SOCKET_URL},
    )))
    factory = Mock(return_value=socket)
    result = CDPClient(ENDPOINT, time.monotonic() + 20, socket_factory=factory, http_client=client)
    return result, factory


def test_transport_events_contexts_and_flattened_session():
    def responder(request):
        return [
            {"method": "Runtime.executionContextCreated", "sessionId": "child", "params": {
                "context": {"id": 14, "auxData": {"isDefault": True, "frameId": "iframe"}},
            }},
            {"id": request["id"], "result": {"result": {"value": {"safe": True}}}},
        ]
    socket = FakeSocket(responder)
    client, factory = transport(socket)
    assert client.evaluate("document.title", "child", 14) == {"safe": True}
    assert client.contexts("child") == {"iframe": 14}
    sent = socket.sent[0]
    assert sent["sessionId"] == "child"
    assert sent["params"]["allowUnsafeEvalBlockedByCSP"] is False
    assert sent["params"]["contextId"] == 14
    assert factory.call_args.kwargs["suppress_origin"] is True
    assert factory.call_args.kwargs["http_no_proxy"] == ["127.0.0.1", "localhost"]
    assert all(0 < timeout <= 5 for timeout in socket.timeouts)
    client.close()
    assert socket.closed


@pytest.mark.parametrize("url", [
    "ws://127.0.0.1:9223/devtools/browser/id", "ws://attacker.test:9222/devtools/browser/id",
    "ws://127.0.0.1:9222/devtools/browser/id?token=secret", "wss://127.0.0.1:9222/devtools/browser/id",
])
def test_discovery_cannot_redirect_socket_outside_verified_endpoint(url):
    factory = Mock()
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"webSocketDebuggerUrl": url},
    )))
    with pytest.raises(TeamsCalendarError):
        CDPClient(ENDPOINT, time.monotonic() + 20, socket_factory=factory, http_client=client)
    factory.assert_not_called()


def test_discovery_does_not_follow_redirects():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        302, headers={"location": "https://attacker.test"},
    )))
    with pytest.raises(TeamsCalendarError):
        CDPClient(ENDPOINT, time.monotonic() + 20, http_client=client)


def test_transport_retains_only_minimal_context_events_not_console_content():
    client, _ = transport(FakeSocket(lambda request: []))
    client._remember_event({"method": "Runtime.consoleAPICalled", "sessionId": "main",
                            "params": {"args": [{"value": "private calendar content"}]}})
    assert not client._events
    client._remember_event({"method": "Runtime.executionContextCreated", "sessionId": "main", "params": {
        "context": {"id": 1, "name": "private-name", "origin": "private-origin", "auxData": {
            "frameId": "frame", "isDefault": True, "private": "unused",
        }},
    }})
    assert client.contexts("main") == {"frame": 1}
    assert "private" not in json.dumps(list(client._events))
    client.close()


def test_protocol_errors_do_not_expose_browser_response():
    socket = FakeSocket(lambda request: [{"id": request["id"], "error": {"message": "private-token"}}])
    client, _ = transport(socket)
    with pytest.raises(TeamsCalendarError) as error:
        client.call("Page.enable")
    assert "private-token" not in str(error.value)
    client.deadline = time.monotonic() - 1
    with pytest.raises(TeamsCalendarError, match="時間切れ"):
        client.call("Page.enable")


def test_websocket_rejects_large_wire_frame_before_payload_allocation():
    wire = io.BytesIO(bytes([0x81, 127]) + struct.pack("!Q", 100 * 1024 * 1024))
    socket = _bounded_socket_class()()
    socket.frame_buffer.recv = wire.read
    with pytest.raises(TeamsCalendarError, match="大きすぎる"):
        socket.recv_frame()
    assert wire.tell() == 10


def test_websocket_rejects_cumulative_fragment_size_before_joining():
    import websocket

    socket = _bounded_socket_class()()
    socket.cont_frame.add(websocket.ABNF(fin=0, opcode=1, data=b"a" * (3 * 1024 * 1024)))
    with pytest.raises(TeamsCalendarError, match="大きすぎる"):
        socket.cont_frame.add(websocket.ABNF(fin=1, opcode=0, data=b"b" * (2 * 1024 * 1024)))
    assert len(socket.cont_frame.cont_data[1]) == 3 * 1024 * 1024


def test_websocket_enforces_deadline_during_fragmented_payload_reads():
    socket = _bounded_socket_class(time.monotonic() - 1)()
    with pytest.raises(TeamsCalendarError, match="時間切れ"):
        socket._recv(1024)


@pytest.mark.parametrize("url,allowed", [
    ("https://teams.microsoft.com/v2/", True),
    ("https://teams.cloud.microsoft/", True),
    ("https://outlook.office.com/hosted/calendar/", True),
    ("https://outlook.office365.com/calendar/", True),
    ("https://outlook.office.com/mail/", False),
    ("https://teams.microsoft.com.attacker.test/", False),
    ("https://user@teams.microsoft.com/", False),
    ("http://teams.microsoft.com/", False),
])
def test_allowed_frame_origins(url, allowed):
    assert _allowed_origin(url, calendar=True) is allowed


class FakeCDP:
    def __init__(self, endpoint=None, deadline=None, *, hidden=False, observed=None):
        self.calls = []
        self.closed = False
        self.hidden = hidden
        self.observed = observed or snapshot()
        self.clicked = []

    def remaining(self):
        return 5

    def close(self):
        self.closed = True

    def contexts(self, session):
        return {"main": 1} if session == "session-main" else {"calendar": 2}

    def children(self, session):
        return []

    def call(self, method, params=None, session=None):
        self.calls.append((method, params, session))
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"targetId": "main", "type": "page", "url": "https://teams.microsoft.com/v2/"},
                {"targetId": "calendar", "type": "iframe", "url": "https://outlook.office.com/hosted/calendar/"},
                {"targetId": "unrelated", "type": "page", "url": "https://outlook.office.com/hosted/calendar/"},
            ]}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-" + params["targetId"]}
        if method == "Page.getFrameTree":
            if session == "session-main":
                return {"frameTree": {"frame": {"id": "main", "url": "https://teams.microsoft.com/v2/"},
                                      "childFrames": [{"frame": {"id": "calendar", "url": "https://outlook.office.com/hosted/calendar/"}}]}}
            return {"frameTree": {"frame": {"id": "calendar", "url": "https://outlook.office.com/hosted/calendar/"}}}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 10}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "iframe-node"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": not self.hidden}}
        return {}

    def evaluate(self, expression, session, context):
        if expression == _SNAPSHOT_JS:
            if session == "session-main":
                return {"url": "https://teams.microsoft.com/v2/", "calendar_present": False, "events": []}
            return self.observed
        self.clicked.append((expression, session))
        return False


def test_reader_attaches_only_teams_descendant_oopif():
    client = FakeCDP()
    result = _CalendarReader(client).read()
    assert result == [client.observed]
    attached = [params["targetId"] for method, params, _ in client.calls if method == "Target.attachToTarget"]
    assert attached == ["main", "calendar"]
    assert client.clicked == []


def test_hidden_calendar_iframe_is_not_read():
    client = FakeCDP(hidden=True)
    with pytest.raises(TeamsCalendarError, match="カレンダーを開いて"):
        _CalendarReader(client).read()
    assert not any(params.get("targetId") == "calendar" for method, params, _ in client.calls if method == "Target.attachToTarget")


@pytest.mark.parametrize("state,can_click", [("idle", False), ("in_meeting", False), ("unknown", False), ("unavailable", False), (None, False)])
def test_all_states_are_read_only(state, can_click):
    client = FakeCDP()
    provider = TeamsDesktopCalendar(Mock(connect_endpoint=Mock(return_value=ENDPOINT)), client_factory=lambda *args: client)
    meetings = provider.fetch_today({"_teams_call_state": state})
    assert len(meetings) == 1
    assert bool(client.clicked) is can_click
    assert provider.status()["coverage"] == "visible_only"
    assert provider.status()["warnings"]
    assert client.closed


def test_confirmed_empty_and_unknown_empty_have_different_result():
    client = FakeCDP(observed=snapshot(events=[], empty_text="No events today"))
    provider = TeamsDesktopCalendar(Mock(connect_endpoint=Mock(return_value=ENDPOINT)), client_factory=lambda *args: client)
    assert provider.fetch_today({}) == []
    assert provider.status()["coverage"] == "complete"
    client.observed["empty_text"] = ""
    with pytest.raises(TeamsCalendarError):
        provider.fetch_today({})
    assert provider.status()["coverage"] == "unknown"


def test_provider_sanitizes_untrusted_error_and_closes():
    client = FakeCDP()
    client.call = Mock(side_effect=RuntimeError("secret-user-data"))
    provider = TeamsDesktopCalendar(Mock(connect_endpoint=Mock(return_value=ENDPOINT)), client_factory=lambda *args: client)
    with pytest.raises(TeamsCalendarError) as error:
        provider.fetch_today({})
    assert "secret-user-data" not in str(error.value)
    assert client.closed
    provider.close()
    with pytest.raises(TeamsCalendarError, match="終了"):
        provider.fetch_today({})


def test_collector_is_read_only():
    assert ".click(" not in _SNAPSHOT_JS
    assert "dispatchEvent" not in _SNAPSHOT_JS
