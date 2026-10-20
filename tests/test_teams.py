import threading

from giji.teams import TeamsDetector, WindowObservation, classify_observations


def window(*ids, count=20, document=True, failed=False):
    return WindowObservation(frozenset(ids), count, document, failed)


def test_hangup_is_positive_even_if_another_window_cannot_be_read():
    result = classify_observations(
        [window(failed=True), window("hangup-button")], teams_running=True
    )
    assert result["state"] == "in_meeting"


def test_native_caption_controls_do_not_establish_idle():
    result = classify_observations(
        [window("Minimize", "Maximize", "Close", count=3, document=False)],
        teams_running=True,
    )
    assert result["state"] == "unknown"


def test_empty_meeting_tree_does_not_stop_recording_when_main_window_is_healthy():
    result = classify_observations(
        [window("app-bar-chat", "app-bar-calls", "app-bar-calendar"),
         window(count=0, document=False)],
        teams_running=True,
    )
    assert result["state"] == "unknown"


def test_readable_tree_without_hangup_is_idle():
    result = classify_observations(
        [window("app-bar-chat")], teams_running=True
    )
    assert result["state"] == "idle"


def test_absent_process_is_idle_but_running_process_with_no_windows_is_unknown():
    assert classify_observations([], teams_running=False)["state"] == "idle"
    assert classify_observations([], teams_running=True)["state"] == "unknown"


def test_provider_exception_is_unknown_and_does_not_leak_details():
    def failure():
        raise RuntimeError("sensitive window title")

    detector = TeamsDetector(failure)
    try:
        result = detector.poll()
        assert result["state"] == "unknown"
        assert "sensitive" not in result["detail"]
    finally:
        detector.close()


def test_timeout_does_not_spawn_unbounded_background_requests():
    released = threading.Event()
    calls = []

    def slow_scan():
        calls.append(1)
        released.wait(2)
        return {"state": "in_meeting", "detail": "connected"}

    detector = TeamsDetector(slow_scan, timeout_seconds=0.01)
    try:
        assert detector.poll()["state"] == "unknown"
        thread = detector._thread
        assert detector.poll()["state"] == "unknown"
        assert detector._thread is thread
        assert calls == [1]
        released.set()
        assert detector._ready.wait(1)
        assert detector.poll()["state"] == "in_meeting"
    finally:
        released.set()
        detector.close()


def test_detection_can_recover_after_temporary_failure():
    results = iter([
        {"state": "unknown", "detail": "transient"},
        {"state": "in_meeting", "detail": "connected"},
        {"state": "idle", "detail": "left"},
    ])
    detector = TeamsDetector(lambda: next(results))
    try:
        assert [detector.poll()["state"] for _ in range(3)] == [
            "unknown", "in_meeting", "idle"
        ]
    finally:
        detector.close()
