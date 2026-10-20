import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from giji.service import GijiService
from giji.store import Store


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


@pytest.fixture(name="service")
def service_fixture(tmp_path):
    calendar = Mock()
    calendar.is_connected.return_value = False
    calendar.fetch_today.return_value = []
    detector = Mock()
    detector.poll.return_value = {"state": "unknown", "detail": "test"}
    recorder = Mock()
    recorder.status.return_value = {
        "seconds": 12, "microphone_level": 0.4, "loopback_level": 0.2, "error": None,
    }
    recorder.stop.return_value = {"duration_seconds": 12}
    recorder.list_devices.return_value = {"microphones": [], "loopbacks": []}
    models = Mock()
    models.status.return_value = {"ready": True, "detail": "test"}
    store = Store(tmp_path / "data")
    result = GijiService(
        store, MemorySecrets(), calendar=calendar, detector=detector,
        recorder=recorder, models=models, pipeline=Mock(),
    )
    yield result
    store.close()


def saved_session(service, sid="test-session", status="ready"):
    folder = service.store.data_dir / "recordings" / sid
    folder.mkdir(parents=True, exist_ok=True)
    session = {
        "id": sid, "meeting_id": None, "title": "テスト会議", "status": status,
        "started_at": "2026-10-20T01:00:00+00:00", "ended_at": "2026-10-20T01:10:00+00:00",
        "duration_seconds": 600, "error": None, "source": "manual", "directory": str(folder),
        "segments": [{"start": 0, "end": 2, "speaker": "SELF", "text": "実際の発言"}],
        "speaker_names": {"SELF": "自分"},
        "minutes": {"summary": "会議の概要", "decisions": [], "action_items": [],
                    "open_questions": [], "topics": []},
    }
    service.store.put_session(session)
    return session


def test_snapshot_never_returns_credentials_or_recording_directory(service):
    service.secrets.set("llm_api_key", "secret-only-in-credential-store")
    original = saved_session(service)
    snapshot = service.snapshot()
    public = snapshot["sessions"][0]
    assert "directory" not in public
    assert "segments" not in public
    assert "minutes" not in public
    assert snapshot["settings"]["has_api_key"] is True
    assert "secret-only-in-credential-store" not in str(snapshot)
    assert service.store.session(original["id"])["directory"] == original["directory"]


@pytest.mark.asyncio
async def test_recording_requires_consent_before_touching_devices(service):
    with pytest.raises(ValueError, match="同意"):
        await service.start_recording()
    service.recorder.start.assert_not_called()
    assert service.store.sessions() == []


@pytest.mark.asyncio
async def test_stop_queues_exactly_one_job_and_keeps_audio_location_private(service):
    service.store.update_settings({"recording_consent": True})
    started = await service.start_recording(title="手動会議")
    sid = started["id"]
    assert started["title"] == "手動会議"
    assert started["status"] == "recording"
    assert "directory" not in started
    assert service.active_id == sid
    with pytest.raises(ValueError, match="すでに"):
        await service.start_recording()
    stopped = await service.stop_recording()
    assert stopped["status"] == "transcribing"
    assert stopped["duration_seconds"] == 12
    assert stopped["ended_at"]
    assert service.active_id is None
    assert service.pending == {sid}
    assert service.queue.get_nowait() == sid
    assert service.queue.empty()
    with pytest.raises(ValueError, match="録音中"):
        await service.stop_recording()
    service.recorder.stop.assert_called_once()


@pytest.mark.asyncio
async def test_open_device_failure_is_journalled_without_secret_exception(service):
    service.store.update_settings({"recording_consent": True})
    service.recorder.start.side_effect = RuntimeError("SECRET native device details")
    with pytest.raises(ValueError, match="デバイス"):
        await service.start_recording()
    assert service.active_id is None
    session = service.store.sessions()[0]
    assert session["status"] == "error"
    assert session["ended_at"]
    assert "SECRET" not in session["error"]
    assert Path(session["directory"]).is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("device_throws", [False, True])
async def test_incomplete_audio_is_preserved_and_not_sent_to_pipeline(service, device_throws):
    service.store.update_settings({"recording_consent": True})
    started = await service.start_recording()
    if device_throws:
        service.recorder.stop.side_effect = RuntimeError("raw native error")
    result = await service.stop_recording(failed=not device_throws)
    assert result["status"] == "error"
    assert service.queue.empty()
    assert service.pending == set()
    assert Path(service.store.session(started["id"])["directory"]).exists()


@pytest.mark.asyncio
async def test_error_reported_during_stop_prevents_processing_incomplete_audio(service):
    service.store.update_settings({"recording_consent": True})
    await service.start_recording()
    service.recorder.status.return_value["error"] = "保存中にディスクの空き容量が不足しました。"
    result = await service.stop_recording()
    assert result["status"] == "error"
    assert service.queue.empty()
    assert service.pending == set()


@pytest.mark.asyncio
async def test_pipeline_exception_keeps_partial_transcript_and_sanitizes_error(service):
    initial = saved_session(service, status="transcribing")
    partial = [{"start": 0, "end": 10, "speaker": "SELF", "text": "保存される発言"}]

    def fail(session, settings, on_update):
        on_update({"segments": partial, "status": "summarizing"})
        raise RuntimeError("SECRET provider request body")

    service.pipeline.process.side_effect = fail
    worker = asyncio.create_task(service._worker())
    try:
        await service.enqueue(initial["id"])
        await asyncio.wait_for(service.queue.join(), timeout=3)
        result = service.store.session(initial["id"])
        assert result["status"] == "error"
        assert result["segments"] == partial
        assert "SECRET" not in result["error"]
        assert service.pending == set()
        assert service.health["pipeline"]["state"] == "idle"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_pipeline_user_actionable_error_is_not_overwritten(service):
    initial = saved_session(service, status="transcribing")

    def fail(session, settings, on_update):
        on_update({"error": "APIキーを設定してください。", "status": "error"})
        raise RuntimeError("private implementation detail")

    service.pipeline.process.side_effect = fail
    worker = asyncio.create_task(service._worker())
    try:
        await service.enqueue(initial["id"])
        await asyncio.wait_for(service.queue.join(), timeout=3)
        assert service.store.session(initial["id"])["error"] == "APIキーを設定してください。"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_restart_marks_unfinished_work_interrupted_without_automatic_api_retry(service):
    unfinished = ("recording", "transcribing", "diarizing", "summarizing")
    for status in (*unfinished, "ready", "error"):
        saved_session(service, sid=status, status=status)
    await service.start()
    try:
        for status in unfinished:
            session = service.store.session(status)
            assert session["status"] == "interrupted"
            assert session["segments"]
        assert service.store.session("ready")["status"] == "ready"
        assert service.store.session("error")["status"] == "error"
        assert service.queue.empty()
        service.pipeline.process.assert_not_called()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_shutdown_preserves_active_recording_for_explicit_retry(service):
    service.store.update_settings({"recording_consent": True})
    started = await service.start_recording()
    await service.close()
    stopped = service.store.session(started["id"])
    assert stopped["status"] == "interrupted"
    assert stopped["duration_seconds"] == 12
    assert service.active_id is None
    assert service.queue.empty()
    service.detector.close.assert_called_once()


@pytest.mark.asyncio
async def test_retry_cannot_duplicate_pending_job(service):
    initial = saved_session(service, status="error")
    result = await service.retry(initial["id"])
    assert result["status"] == "transcribing"
    with pytest.raises(ValueError, match="再処理"):
        await service.retry(initial["id"])
    with pytest.raises(ValueError, match="削除"):
        service.delete_session(initial["id"])
    assert service.queue.qsize() == 1


@pytest.mark.asyncio
async def test_invalid_settings_do_not_mutate_existing_api_key(service):
    service.secrets.set("llm_api_key", "existing")
    with pytest.raises(ValueError, match="同意"):
        await service.update_settings({"api_key": "replacement", "auto_record": True})
    assert service.secrets.get("llm_api_key") == "existing"
    assert service.store.settings()["auto_record"] is False


@pytest.mark.asyncio
async def test_microsoft_configuration_change_clears_old_account_calendar(service):
    service.calendar.is_connected.return_value = True
    service.store.add_meeting({
        "id": "old-account-event", "title": "old private event",
        "start": "2026-10-20T01:00:00+00:00", "end": "2026-10-20T02:00:00+00:00",
    })
    await service.update_settings({"client_id": "a8a61be9-d04a-44be-843e-5c9591734553"})
    service.calendar.logout.assert_called_once()
    assert service.store.meetings() == []


def test_safe_directory_rejects_arbitrary_external_path(service, tmp_path):
    session = saved_session(service)
    external = tmp_path / "outside"
    external.mkdir()
    session["directory"] = str(external)
    with pytest.raises(ValueError, match="保存先"):
        service.safe_directory(session)


@pytest.mark.asyncio
async def test_failed_sync_keeps_previous_calendar_and_reports_error(service):
    service.calendar.is_connected.return_value = True
    meeting = {"id": "saved", "start": "2026-10-20T01:00:00+00:00"}
    service.store.add_meeting(meeting)
    service.calendar.fetch_today.side_effect = RuntimeError("private token")
    await service.sync()
    assert service.store.meetings() == [meeting]
    assert service.health["calendar"]["state"] == "error"
    assert "private token" not in str(service.health)


@pytest.mark.asyncio
async def test_default_desktop_sync_does_not_require_microsoft_login(service):
    assert service.store.settings()["calendar_source"] == "outlook_desktop"
    assert service.store.settings()["client_id"] == ""
    service.calendar.is_connected.return_value = False
    meeting = {"id": "desktop-meeting", "start": "2026-10-20T01:00:00+00:00"}
    service.calendar.fetch_today.return_value = [meeting]
    await service.sync()
    service.calendar.fetch_today.assert_called_once()
    assert service.store.meetings() == [meeting]
    assert service.snapshot()["microsoft"]["connected"] is True
    assert service.snapshot()["microsoft"]["provider"] == "outlook_desktop"
    assert service.public_settings()["has_microsoft_login"] is False


@pytest.mark.asyncio
async def test_graph_still_requires_login_and_is_not_silently_used_as_fallback(service):
    service.store.update_settings({"calendar_source": "graph"})
    service.calendar.is_connected.return_value = False
    await service.sync()
    service.calendar.fetch_today.assert_not_called()
    assert service.health["calendar"]["state"] == "disconnected"


@pytest.mark.asyncio
async def test_desktop_mode_never_starts_microsoft_device_login(service):
    with pytest.raises(ValueError, match="サインイン不要"):
        await service.login()
    service.calendar.begin_login.assert_not_called()


@pytest.mark.asyncio
async def test_changing_source_clears_stale_meetings_but_keeps_recordings(service):
    saved_session(service)
    service.store.add_meeting({"id": "old", "start": "2026-10-20T01:00:00+00:00"})
    await service.update_settings({"calendar_source": "graph"})
    assert service.store.meetings() == []
    assert len(service.store.sessions()) == 1
    assert service.snapshot()["microsoft"]["provider"] == "graph"
    assert service.health["calendar"]["last_sync"] is None


@pytest.mark.asyncio
async def test_safe_desktop_error_is_visible_and_keeps_cached_meetings(service):
    from giji.microsoft import CalendarError

    service.store.add_meeting({"id": "old", "start": "2026-10-20T01:00:00+00:00"})
    service.calendar.fetch_today.side_effect = CalendarError("従来版Outlookを起動してください。")
    await service.sync()
    assert service.health["calendar"]["detail"] == "従来版Outlookを起動してください。"
    assert len(service.store.meetings()) == 1


@pytest.mark.asyncio
async def test_start_drops_v01_calendar_cache_without_removing_saved_minutes(service):
    saved_session(service)
    service.store.add_meeting({"id": "old-graph-account", "start": "2026-10-20T01:00:00+00:00"})
    await service.start()
    try:
        assert service.store.meetings() == []
        assert len(service.store.sessions()) == 1
    finally:
        await service.close()


def test_today_view_excludes_previous_day_even_when_sync_fails(service):
    now = datetime(2026, 10, 20, 12, tzinfo=timezone(timedelta(hours=9)))
    service.store.add_meeting({
        "id": "yesterday", "start": "2026-10-19T01:00:00+00:00", "end": "2026-10-19T02:00:00+00:00"})
    service.store.add_meeting({
        "id": "today", "start": "2026-10-20T01:00:00+00:00", "end": "2026-10-20T02:00:00+00:00"})
    service.health["calendar"]["state"] = "error"
    assert [m["id"] for m in service.today_meetings(now)] == ["today"]


def test_automatic_recording_does_not_bind_to_failed_sync_cache(service):
    now = datetime.now(timezone.utc)
    service.store.add_meeting({
        "id": "old-account", "start": now.isoformat(), "end": (now + timedelta(minutes=10)).isoformat()})
    service.health["calendar"].update(state="error", last_sync=now.isoformat())
    assert service.matchable_meetings() == []
    service.armed_id = "old-account"
    assert service.matchable_meetings()[0]["id"] == "old-account"


@pytest.mark.asyncio
async def test_logout_clears_calendar_and_credentials(service):
    service.store.add_meeting({"id": "saved", "start": "2026-10-20T01:00:00+00:00"})
    await service.logout()
    service.calendar.logout.assert_called_once()
    assert service.store.meetings() == []
    assert service.health["calendar"]["last_sync"] is None
