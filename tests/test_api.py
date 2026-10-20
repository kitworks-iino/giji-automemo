import wave
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from giji.api import create_app
from test_service import saved_session, service_fixture  # noqa: F401


PORT = 48888
TOKEN = "local-test-session-token"


@pytest.fixture
def client(service, tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>giji</title>", encoding="utf-8")
    app = create_app(service, TOKEN, PORT, run_background=False, frontend_dir=ui)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        yield client


def auth(**extra):
    return {"Authorization": f"Bearer {TOKEN}", **extra}


def test_loopback_api_requires_runtime_token(client):
    assert client.get("/api/snapshot").status_code == 401
    assert client.get("/api/snapshot", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/snapshot", headers=auth()).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:1", "127.0.0.1:48888.evil.example"])
def test_dns_rebinding_host_is_rejected(client, host):
    assert client.get("/api/snapshot", headers=auth(Host=host)).status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:1"])
def test_cross_origin_mutation_is_rejected_even_with_token(client, origin):
    response = client.post("/api/demo", headers=auth(Origin=origin))
    assert response.status_code == 403


def test_same_origin_mutation_and_security_headers(client):
    response = client.post("/api/demo", headers=auth(Origin=f"http://127.0.0.1:{PORT}"))
    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    root = client.get("/")
    assert root.status_code == 200
    assert TOKEN not in root.text


def test_credentials_remain_out_of_json_and_database(client, service):
    secret = "test-key-that-must-never-be-returned"
    response = client.post("/api/settings", headers=auth(), json={"api_key": secret})
    assert response.status_code == 200
    assert response.json()["has_api_key"] is True
    assert secret not in response.text
    assert secret not in client.get("/api/snapshot", headers=auth()).text
    assert service.secrets.get("llm_api_key") == secret
    assert secret not in str(service.store.settings())


def test_partial_settings_patch_does_not_reset_unrelated_settings(client, service):
    service.store.update_settings({"llm_model": "existing-model", "recording_consent": True})
    response = client.post("/api/settings", headers=auth(), json={"sync_interval_seconds": 120})
    assert response.status_code == 200
    assert response.json()["llm_model"] == "existing-model"
    assert response.json()["recording_consent"] is True
    assert response.json()["sync_interval_seconds"] == 120


@pytest.mark.parametrize("patch", [
    {"unknown_setting": True},
    {"client_id": "not-a-client-id"},
    {"tenant_id": ""},
    {"whisper_model": "../../outside"},
    {"language": "ja; shell"},
    {"sync_interval_seconds": 1},
    {"end_grace_seconds": 0},
    {"microphone_id": -1},
    {"llm_base_url": "http://api.example/v1"},
    {"llm_base_url": "https://secret@example.com/v1"},
    {"llm_base_url": "https://example.com/v1?api_key=secret"},
])
def test_settings_reject_invalid_or_unsafe_values(client, patch):
    assert client.post("/api/settings", headers=auth(), json=patch).status_code == 422


def test_missing_sessions_are_404(client):
    for path in ("", "/export", "/audio/system"):
        assert client.get(f"/api/sessions/missing{path}", headers=auth()).status_code == 404
    assert client.post("/api/sessions/missing/retry", headers=auth()).status_code == 404


def test_public_session_and_markdown_export_have_no_internal_path(client, service):
    session = saved_session(service)
    service.store.patch_session(session["id"], {
        "title": '会議\r\nInjected: header / <script>alert(1)</script>',
        "minutes": {"summary": "<script>bad()</script>", "decisions": [], "action_items": [],
                    "open_questions": [], "topics": []},
    })
    detail = client.get(f"/api/sessions/{session['id']}", headers=auth())
    assert detail.status_code == 200
    assert "directory" not in detail.json()
    exported = client.get(f"/api/sessions/{session['id']}/export", headers=auth())
    assert exported.status_code == 200
    assert "text/markdown" in exported.headers["content-type"]
    assert "<script>" not in exported.text
    assert "directory" not in exported.text
    assert "\r" not in exported.headers["content-disposition"]
    assert "\n" not in exported.headers["content-disposition"]
    assert exported.headers["content-disposition"].startswith("attachment;")


def test_audio_is_authenticated_and_restricted_to_known_tracks(client, service):
    session = saved_session(service)
    folder = service.safe_directory(session)
    with wave.open(str(folder / "system.wav"), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\0" * 320)
    route = f"/api/sessions/{session['id']}/audio"
    assert client.get(f"{route}/system").status_code == 401
    response = client.get(f"{route}/system", headers=auth())
    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"
    assert client.get(f"{route}/capture.json", headers=auth()).status_code == 404
    assert client.get(f"{route}/microphone", headers=auth()).status_code == 404


def test_signed_audio_url_supports_seeking_and_is_scoped_to_one_track(client, service):
    session = saved_session(service, sid="1234567890abcdef1234567890abcdef")
    folder = service.safe_directory(session)
    for track in ("microphone", "system"):
        with wave.open(str(folder / f"{track}.wav"), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16000)
            target.writeframes(b"\0" * 320)
    detail = client.get(f"/api/sessions/{session['id']}", headers=auth()).json()
    playback = detail["audio_urls"]["system"]
    assert TOKEN not in playback
    assert client.get(playback).status_code == 200
    partial = client.get(playback, headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert partial.content == b"RIFF"
    assert partial.headers["content-range"].startswith("bytes 0-3/")
    signature = parse_qs(urlsplit(playback).query)["playback"][0]
    other_track = playback.replace("/audio/system?", "/audio/microphone?")
    assert client.get(other_track).status_code == 401
    other_session = playback.replace(session["id"], "abcdef1234567890abcdef1234567890")
    assert client.get(other_session).status_code == 401
    assert client.get(f"/api/snapshot?playback={signature}").status_code == 401
    assert client.post(playback).status_code == 401
    assert client.get(playback, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get(
        f"/api/sessions/{session['id']}/audio/microphone",
        headers=auth(Range="bytes=0-3"),
    ).status_code == 206


def test_audio_path_outside_data_directory_is_rejected(client, service, tmp_path):
    session = saved_session(service)
    service.store.patch_session(session["id"], {"directory": str(tmp_path)})
    response = client.get(f"/api/sessions/{session['id']}/audio/system", headers=auth())
    assert response.status_code == 400
    assert str(tmp_path) not in response.text


def test_speaker_names_only_accept_real_labels_and_are_trimmed(client, service):
    session = saved_session(service)
    route = f"/api/sessions/{session['id']}/speakers"
    response = client.post(route, headers=auth(), json={"speaker_names": {"SELF": "  田中  "}})
    assert response.status_code == 200
    assert response.json()["speaker_names"]["SELF"] == "田中"
    assert client.post(route, headers=auth(), json={"speaker_names": {"unknown": "誰か"}}).status_code == 400
    assert client.post(route, headers=auth(), json={"speaker_names": {"SELF": " "}}).status_code == 422
    service.pending.add(session["id"])
    assert client.post(route, headers=auth(), json={"speaker_names": {"SELF": "田中"}}).status_code == 400


@pytest.mark.parametrize("url", [
    "https://teams.microsoft.com:444/meet/123",
    "https://teams.microsoft.com",
    "https://teams.microsoft.com@evil.example/meet/123",
])
def test_join_rejects_nonstandard_or_spoofed_teams_urls(client, service, url):
    service.store.add_meeting({
        "id": "join-test", "title": "会議", "start": "2026-10-20T01:00:00+00:00",
        "end": "2026-10-20T02:00:00+00:00", "join_url": url, "cancelled": False,
    })
    response = client.post("/api/meetings/join-test/join", headers=auth())
    assert response.status_code == 400
    assert service.armed_id is None


def test_valid_join_arms_the_chosen_meeting(client, service):
    url = "https://teams.microsoft.com/l/meetup-join/test"
    service.store.add_meeting({
        "id": "join-test", "title": "会議", "start": "2026-10-20T01:00:00+00:00",
        "end": "2026-10-20T02:00:00+00:00", "join_url": url, "cancelled": False,
    })
    response = client.post("/api/meetings/join-test/join", headers=auth())
    assert response.status_code == 200
    assert response.json()["join_url"] == url
    assert service.armed_id == "join-test"


def test_demo_is_explicit_and_cannot_be_recorded_or_joined(client):
    assert client.get("/api/snapshot", headers=auth()).json()["meetings"] == []
    assert client.post("/api/demo", headers=auth()).status_code == 200
    snapshot = client.get("/api/snapshot", headers=auth()).json()
    assert all("デモ" in m["title"] for m in snapshot["meetings"])
    assert client.post("/api/meetings/demo-0/join", headers=auth()).status_code == 400
    assert client.post("/api/sessions/demo-session/retry", headers=auth()).status_code == 400
    assert client.delete("/api/demo", headers=auth()).status_code == 200
    assert client.get("/api/snapshot", headers=auth()).json()["meetings"] == []
