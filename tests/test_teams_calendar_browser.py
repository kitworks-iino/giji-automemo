"""Optional real Chromium CDP tests against synthetic, intercepted pages.

No Teams account, network calendar endpoint, cookies or real user data is used.
These tests prove protocol/DOM behavior, not a Teams version's compatibility.
Install Playwright + Chromium to run; the portable package does not need them.
"""

import shutil
import socket
import time
from datetime import datetime, timezone

import pytest

from giji.teams_calendar import CDPClient, TeamsCalendarError, _CalendarReader, _SNAPSHOT_JS
from giji.teams_calendar_parser import CalendarParseError, parse_calendar_snapshot


playwright = pytest.importorskip("playwright.sync_api")
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROMIUM, reason="Optional Chromium CDP harness")


@pytest.fixture
def browser_calendar():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(
            executable_path=CHROMIUM, headless=True,
            args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                  "--site-per-process", "--no-sandbox"],
        )
        context = browser.new_context(timezone_id="UTC", viewport={"width": 1200, "height": 900})
        markup = {"calendar": "<p>Calendar fixture not loaded</p>", "frame_url": "https://outlook.office.com/hosted/calendar/"}

        def serve(route):
            url = route.request.url
            if url.startswith("https://teams.microsoft.com/v2/"):
                html = '<html><body><h1>Synthetic Teams shell</h1><iframe title="Calendar" style="width:1000px;height:750px" src="' + markup["frame_url"] + '"></iframe></body></html>'
            elif url.startswith(("https://outlook.office.com/hosted/calendar/", "https://teams.microsoft.com/embedded-calendar")):
                html = "<html><body>" + markup["calendar"] + "</body></html>"
            else:
                route.abort()
                return
            route.fulfill(status=200, content_type="text/html", body=html)

        context.route("**/*", serve)
        page = context.new_page()
        clients = []

        def load(html, same_process=False):
            markup["calendar"] = html
            if same_process:
                markup["frame_url"] = "https://teams.microsoft.com/embedded-calendar"
            page.goto("https://teams.microsoft.com/v2/", wait_until="networkidle")
            client = CDPClient(f"http://127.0.0.1:{port}", time.monotonic() + 15)
            clients.append(client)
            return client, page

        yield load
        for client in clients:
            client.close()
        browser.close()


def dated_markup(body):
    today = datetime.now(timezone.utc).date().isoformat()
    return f'<h2>{today}</h2><button aria-pressed="true">Day</button><button>Today</button><div role="grid" aria-label="Calendar">{body}</div>'


def test_real_cdp_reads_out_of_process_calendar_and_actual_js(browser_calendar):
    client, _ = browser_calendar(dated_markup(
        '<button data-event-id="review" aria-label="Synthetic review, 10:00 to 11:00">Review</button>'
    ))
    reader = _CalendarReader(client)
    frames = reader.frames()
    calendars = [frame for frame in frames if "outlook.office.com" in frame["url"]]
    assert len(calendars) == 1
    assert len({frame["session"] for frame in frames}) == 2
    observed = reader.snapshot(calendars[0])
    assert observed["calendar_present"]
    assert observed["events"][0]["key"] == "review"
    parsed = parse_calendar_snapshot(observed)
    assert parsed["meetings"][0]["title"] == "Synthetic review"
    assert parsed["coverage"] == "visible_only"


def test_real_dom_does_not_infer_interval_from_two_unrelated_times(browser_calendar):
    today = datetime.now(timezone.utc).date().isoformat()
    client, _ = browser_calendar(dated_markup(
        f'<article><time datetime="{today}T10:00:00Z">10:00</time>First</article>'
        f'<article><time datetime="{today}T11:00:00Z">11:00</time>Second</article>'
    ))
    reader = _CalendarReader(client)
    frame = next(frame for frame in reader.frames() if "outlook.office.com" in frame["url"])
    observed = client.evaluate(_SNAPSHOT_JS, frame["session"], frame["context"])
    assert observed["events"] == []


def test_real_dom_keyed_parent_and_nested_button_are_one_event(browser_calendar):
    client, _ = browser_calendar(dated_markup(
        '<div data-event-id="review"><button aria-label="Synthetic review, 10:00 to 11:00">Synthetic review, 10:00 to 11:00</button></div>'
    ))
    observed = _CalendarReader(client).read()[0]
    assert len(observed["events"]) == 1
    assert len(parse_calendar_snapshot(observed)["meetings"]) == 1


def test_real_dom_undated_event_prevents_false_complete_empty(browser_calendar):
    client, _ = browser_calendar(dated_markup(
        '<div data-event-id="unknown">All-day visible event</div><aside><p>No events today</p></aside>'
    ))
    observed = _CalendarReader(client).read()[0]
    assert len(observed["events"]) == 1
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(observed)


def test_today_marker_in_mini_calendar_cannot_date_time_only_event(browser_calendar):
    today = datetime.now(timezone.utc).date().isoformat()
    client, _ = browser_calendar(
        f'<aside><button aria-current="date" data-date="{today}">{today}</button></aside>'
        '<button aria-pressed="true">Day</button><button>Today</button>'
        '<div role="grid" aria-label="Calendar"><h2>22</h2><button aria-label="Synthetic review, 10:00 to 11:00">Review</button></div>'
    )
    observed = _CalendarReader(client).read()[0]
    assert today not in observed["date_texts"]
    with pytest.raises(CalendarParseError):
        parse_calendar_snapshot(observed)


def test_real_cdp_same_process_iframe_has_its_own_execution_context(browser_calendar):
    client, _ = browser_calendar(dated_markup(
        '<button data-event-id="review" aria-label="Same-process review, 10:00 to 11:00">Review</button>'
    ), same_process=True)
    reader = _CalendarReader(client)
    frames = reader.frames()
    assert len(frames) == 2
    assert len({frame["session"] for frame in frames}) == 1
    assert len({frame["context"] for frame in frames}) == 2
    meetings = parse_calendar_snapshot(reader.read()[0])["meetings"]
    assert meetings[0]["title"] == "Same-process review"


def test_real_cdp_hidden_iframe_and_unrelated_outlook_tab_are_excluded(browser_calendar):
    client, page = browser_calendar(dated_markup(
        '<button data-event-id="review" aria-label="Hidden review, 10:00 to 11:00">Review</button>'
    ))
    page.locator("iframe").evaluate("element => element.style.display = 'none'")
    orphan = page.context.new_page()
    orphan.goto("https://outlook.office.com/hosted/calendar/?orphan=true")
    reader = _CalendarReader(client)
    assert len(reader.frames()) == 1
    with pytest.raises(TeamsCalendarError, match="カレンダーを開いて"):
        reader.read()


def test_visible_event_container_does_not_read_hidden_child_text(browser_calendar):
    client, _ = browser_calendar(dated_markup(
        '<div data-event-id="empty" style="width:200px;height:30px"><span style="display:none">Private hidden title, 10:00 to 11:00</span></div>'
    ))
    observed = _CalendarReader(client).read()[0]
    assert len(observed["events"]) == 1
    assert observed["events"][0]["label"] == ""
    assert observed["events"][0]["text"] == ""

