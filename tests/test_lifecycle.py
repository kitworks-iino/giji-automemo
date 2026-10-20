from datetime import datetime, timedelta, timezone

from giji.lifecycle import MeetingLifecycle, match_meeting


def test_short_meeting_ends_after_observed_exit_not_calendar():
    state = MeetingLifecycle()
    assert state.step("in_meeting", False, True, 0, 12) == "start"
    state.on_started(True)
    assert state.step("in_meeting", True, True, 999999, 12) is None
    assert state.step("idle", True, True, 1000000, 12) is None
    assert state.step("idle", True, True, 1000012, 12) == "stop"


def test_unknown_and_reconnect_never_end_recording():
    state = MeetingLifecycle()
    state.on_started(True)
    assert state.step("idle", True, True, 0, 12) is None
    assert state.step("unknown", True, True, 10, 12) is None
    assert state.step("idle", True, True, 15, 12) is None
    assert state.step("in_meeting", True, True, 25, 12) is None
    assert state.step("unavailable", True, True, 100, 12) is None


def test_manual_stop_does_not_immediately_restart():
    state = MeetingLifecycle()
    state.on_started(True)
    state.on_stopped()
    assert state.step("in_meeting", False, True, 0, 12) is None
    assert state.step("idle", False, True, 5, 12) is None
    assert state.step("idle", False, True, 17, 12) is None
    assert state.step("in_meeting", False, True, 18, 12) == "start"


def test_manual_recording_without_teams_is_not_ended():
    state = MeetingLifecycle()
    state.on_started(False)
    assert state.step("idle", True, False, 0, 12) is None
    assert state.step("idle", True, False, 20, 12) is None


def test_match_ignores_demo_cancelled_and_ambiguous_candidates():
    now = datetime.now(timezone.utc)
    meeting = {"id": "real", "start": (now - timedelta(minutes=5)).isoformat(),
               "end": (now + timedelta(minutes=10)).isoformat(), "cancelled": False}
    demo = {**meeting, "id": "demo-0"}
    canceled = {**meeting, "id": "canceled", "cancelled": True}
    assert match_meeting([meeting, demo, canceled], now) == meeting
    other = {**meeting, "id": "other"}
    assert match_meeting([meeting, other], now) is None
    assert match_meeting([meeting, other], now, "real") == meeting
    assert match_meeting([meeting], now + timedelta(days=1)) is None
