from copy import deepcopy
import json

import httpx
import pytest

from giji.models import ModelManager
from giji.pipeline import (
    Pipeline,
    PipelineError,
    assign_speakers,
    render_markdown,
    transcript_chunks,
    validate_minutes,
)


MINUTES = {
    "summary": "要約",
    "decisions": ["リリース日を確認する"],
    "action_items": [{"task": "確認", "owner": None, "due_date": None}],
    "open_questions": ["日程"],
    "topics": [{"title": "リリース", "notes": ["日程は未定"]}],
}


class Secrets:
    def __init__(self, key="test-only-key"):
        self.key = key

    def get(self, name):
        assert name == "llm_api_key"
        return self.key


class Models:
    def status(self, settings):
        return {"ready": True}


def session(tmp_path):
    return {
        "id": "test-session",
        "title": "会議",
        "directory": str(tmp_path),
        "segments": [],
        "speaker_names": {},
    }


def test_chunking_never_drops_long_utterance():
    text = "発言内容" * 20_000
    chunks = transcript_chunks([{"start": 0, "speaker": "SELF", "text": text}], {}, limit=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    restored = "".join(chunk.split(": ", 1)[1] for chunk in chunks)
    assert restored == text


def test_speaker_assignment_splits_on_word_boundary_and_preserves_self():
    segments = [
        {"start": 0, "end": 2, "speaker": "SPEAKER_00", "text": "hello world", "words": [
            {"start": 0, "end": 0.9, "word": "hello"},
            {"start": 1.1, "end": 2, "word": " world"},
        ]},
        {"start": 0.5, "end": 0.8, "speaker": "SELF", "text": "はい"},
        {"start": 4, "end": 5, "speaker": "SPEAKER_00", "text": "unknown"},
    ]
    turns = [
        {"start": 0, "end": 1, "speaker": "SPEAKER_00"},
        {"start": 1, "end": 2, "speaker": "SPEAKER_01"},
    ]
    output = assign_speakers(segments, turns)
    assert [row["speaker"] for row in output] == ["SPEAKER_00", "SELF", "SPEAKER_01", "SPEAKER_UNKNOWN"]
    assert output[1]["text"] == "はい"


def test_no_cloud_consent_preserves_transcript_without_request(tmp_path, monkeypatch):
    pipeline = Pipeline(Models(), Secrets())
    monkeypatch.setattr(pipeline, "_transcribe", lambda *args: [
        {"start": 0, "end": 1, "speaker": "SELF", "text": "保存される発言"}
    ])
    monkeypatch.setattr(pipeline, "_summarize", lambda *args: pytest.fail("Cloud called without consent"))
    updates = []
    result = pipeline.process(session(tmp_path), {"diarization": False, "cloud_consent": False}, updates.append)
    assert result["status"] == "error"
    assert result["segments"][0]["text"] == "保存される発言"
    assert result["transcription_complete"] is True
    assert any("segments" in update for update in updates)


def test_retry_reuses_saved_transcript_and_diarization(tmp_path, monkeypatch):
    pipeline = Pipeline(Models(), Secrets())
    existing = session(tmp_path) | {
        "transcription_complete": True,
        "diarization_complete": True,
        "segments": [{"start": 0, "end": 1, "speaker": "SPEAKER_01", "text": "発言"}],
    }
    monkeypatch.setattr(pipeline, "_transcribe", lambda *args: pytest.fail("ASR repeated"))
    monkeypatch.setattr(pipeline, "_diarize", lambda *args: pytest.fail("Diarization repeated"))
    monkeypatch.setattr(pipeline, "_summarize", lambda *args: deepcopy(MINUTES))
    result = pipeline.process(existing, {"diarization": True, "cloud_consent": True}, lambda _: None)
    assert result["status"] == "ready"
    assert result["minutes"] == MINUTES


def test_diarization_failure_retains_raw_transcript_for_retry(tmp_path, monkeypatch):
    pipeline = Pipeline(Models(), Secrets())
    monkeypatch.setattr(pipeline, "_transcribe", lambda *args: [
        {"start": 0, "end": 1, "speaker": "SPEAKER_00", "text": "発言"}
    ])
    def fail(_):
        raise RuntimeError("Sensitive path or content should never be shown")
    monkeypatch.setattr(pipeline, "_diarize", fail)
    result = pipeline.process(session(tmp_path), {"diarization": True}, lambda _: None)
    assert result["transcription_complete"]
    assert not result["diarization_complete"]
    assert result["segments"][0]["text"] == "発言"
    assert "Sensitive" not in result["error"]


def test_silence_does_not_call_llm(tmp_path, monkeypatch):
    pipeline = Pipeline(Models(), Secrets(None))
    monkeypatch.setattr(pipeline, "_transcribe", lambda *args: [])
    monkeypatch.setattr(pipeline, "_summarize", lambda *args: pytest.fail("No need for LLM"))
    result = pipeline.process(session(tmp_path), {"diarization": False}, lambda _: None)
    assert result["status"] == "ready"
    assert "発話を検出" in result["minutes"]["summary"]


def test_llm_output_validation_does_not_invent_metadata():
    assert validate_minutes(MINUTES)["action_items"][0]["owner"] is None
    with pytest.raises(PipelineError):
        validate_minutes({"summary": "incomplete"})
    invalid = deepcopy(MINUTES)
    invalid["action_items"] = [{"task": "確認", "owner": ["invalid"]}]
    with pytest.raises(PipelineError):
        validate_minutes(invalid)


def test_api_error_never_echoes_server_body(monkeypatch):
    def respond(request):
        assert request.headers["authorization"] == "Bearer test-only-key"
        return httpx.Response(401, json={"error": "SECRET-API-KEY and PRIVATE-TRANSCRIPT"})
    original_client = httpx.Client
    monkeypatch.setattr("giji.pipeline.httpx.Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    with pytest.raises(PipelineError) as caught:
        Pipeline._complete("PRIVATE-TRANSCRIPT", {}, "test-only-key")
    assert "SECRET" not in str(caught.value)
    assert "PRIVATE" not in str(caught.value)
    assert "認証" in str(caught.value)


def test_api_success_and_no_audio_in_payload(monkeypatch):
    def respond(request):
        body = json.loads(request.content)
        assert body["messages"][1]["content"] == "文字起こしだけ"
        assert request.url.path == "/v1/chat/completions"
        assert set(body) == {"model", "messages", "response_format", "temperature", "max_tokens"}
        return httpx.Response(200, json={"choices": [{
            "message": {"content": json.dumps(MINUTES)}, "finish_reason": "stop",
        }]})
    original_client = httpx.Client
    monkeypatch.setattr("giji.pipeline.httpx.Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    assert Pipeline._complete("文字起こしだけ", {}, "test-only-key") == MINUTES


def test_rate_limit_retries_are_bounded(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, json={"error": "not echoed"})
    original_client = httpx.Client
    monkeypatch.setattr("giji.pipeline.httpx.Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    monkeypatch.setattr("giji.pipeline.time.sleep", lambda _: None)
    with pytest.raises(PipelineError):
        Pipeline._complete("text", {}, "test-only-key")
    assert len(calls) == 3


def test_long_transcript_is_reduced_without_omitting_chunks(monkeypatch):
    pipeline = Pipeline(Models(), Secrets())
    calls = []
    def complete(content, settings, key):
        calls.append(content)
        return deepcopy(MINUTES)
    monkeypatch.setattr(pipeline, "_complete", complete)
    segments = [{"start": n, "speaker": "SELF", "text": f"unique{n}-" + "あ" * 8000} for n in range(5)]
    result = pipeline._summarize(segments, {}, {"cloud_consent": True}, "test-only-key")
    assert result == MINUTES
    assert len(calls) == 6
    for n in range(5):
        assert any(f"unique{n}-" in content for content in calls)


def test_consent_revoked_during_local_inference_prevents_cloud(tmp_path, monkeypatch):
    current = {"cloud_consent": True, "diarization": False}
    pipeline = Pipeline(Models(), Secrets(), settings_provider=lambda: dict(current))
    def transcribe(*args):
        current["cloud_consent"] = False
        return [{"start": 0, "end": 1, "speaker": "SELF", "text": "保存のみ"}]
    monkeypatch.setattr(pipeline, "_transcribe", transcribe)
    monkeypatch.setattr(pipeline, "_complete", lambda *args: pytest.fail("Revoked consent ignored"))
    result = pipeline.process(session(tmp_path), dict(current), lambda _: None)
    assert result["status"] == "error"
    assert result["segments"][0]["text"] == "保存のみ"


def test_consent_rechecked_between_summary_chunks(monkeypatch):
    current = {"cloud_consent": True}
    pipeline = Pipeline(Models(), Secrets(), settings_provider=lambda: dict(current))
    calls = []
    def complete(*args):
        calls.append(args)
        current["cloud_consent"] = False
        return deepcopy(MINUTES)
    monkeypatch.setattr(pipeline, "_complete", complete)
    segments = [{"start": n, "speaker": "SELF", "text": "あ" * 8000} for n in range(3)]
    with pytest.raises(PipelineError, match="外部送信が停止"):
        pipeline._summarize(segments, {}, {"cloud_consent": True}, "key")
    assert len(calls) == 1


def test_api_url_requires_tls_and_rejects_embedded_credentials():
    for url in ("http://example.com/v1", "https://user:password@example.com/v1", "https://example.com/v1?api_key=x"):
        with pytest.raises(PipelineError):
            Pipeline._complete("text", {"llm_base_url": url}, "key")


def test_export_escapes_html_links_and_resolves_speakers():
    markdown = render_markdown({
        "title": "<script>alert(1)</script>",
        "minutes": MINUTES,
        "speaker_names": {"SPEAKER_00": "佐藤"},
        "segments": [{"start": 61, "speaker": "SPEAKER_00", "text": "[link](https://example.com)"}],
    })
    assert "<script>" not in markdown
    assert "&lt;script&gt;" in markdown
    assert "00:01:01 佐藤" in markdown
    assert "\\[link\\]" in markdown
    assert "未指定" in markdown


def test_model_status_requires_completed_files(tmp_path):
    manager = ModelManager(tmp_path)
    settings = {"whisper_model": "small", "diarization": False}
    path = manager.whisper_path(settings)
    path.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (path / name).write_text("x")
    assert manager.status(settings)["ready"] is False
    (path / ".complete").write_text("1")
    assert manager.status(settings)["ready"] is True
    assert manager.status(settings | {"diarization": True})["ready"] is False
    assert manager.status({"whisper_model": "../unsafe"})["ready"] is False
