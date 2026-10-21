from copy import deepcopy
import json
import sys
from types import SimpleNamespace
import wave

import httpx
import numpy as np
import pytest

from giji.models import ModelManager, WHISPER_ENGINE
from giji.pipeline import (
    ASR_MAX_SECONDS,
    ASR_SAMPLE_RATE,
    Pipeline,
    PipelineError,
    assign_speakers,
    pcm16_blocks,
    render_markdown,
    speech_chunks,
    transcript_chunks,
    validate_minutes,
    whisper_segments,
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
    for name in ("encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt", "silero_vad.onnx"):
        (path / name).write_text("x")
    assert manager.status(settings)["ready"] is False
    (path / ".complete").write_text(json.dumps({"engine": WHISPER_ENGINE, "model": "small"}))
    assert manager.status(settings)["ready"] is True
    assert manager.status(settings | {"diarization": True})["ready"] is False
    assert manager.status({"whisper_model": "../unsafe"})["ready"] is False


def write_pcm(path, samples, rate=16000, channels=1, width=2):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(np.asarray(samples, dtype="<i2").tobytes())


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
def test_pcm_blocks_downmix_and_resample_without_long_track_drift(tmp_path, rate):
    # Opposite-polarity stereo proves downmixing instead of flattening channels.
    mono = (np.sin(np.arange(rate * 3 + rate // 2) * 0.1) * 20000).astype("<i2")
    stereo = np.column_stack((mono, -mono))
    path = tmp_path / "audio.wav"
    write_pcm(path, stereo, rate, channels=2)
    blocks = list(pcm16_blocks(path))
    assert [len(block) for block in blocks] == [16000, 16000, 16000, 8000]
    assert all(block.dtype == np.float32 for block in blocks)
    assert np.max(np.abs(np.concatenate(blocks))) == 0


def test_pcm_reader_rejects_missing_unsupported_and_truncated_audio(tmp_path):
    path = tmp_path / "audio.wav"
    with pytest.raises(PipelineError, match="不足"):
        list(pcm16_blocks(path))
    write_pcm(path, [1, 2, 3], width=1)
    with pytest.raises(PipelineError, match="録音形式"):
        list(pcm16_blocks(path))
    write_pcm(path, np.ones(100, dtype="<i2"))
    # Even a whole-frame truncation must be detected against the WAV header.
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(PipelineError, match="途中"):
        list(pcm16_blocks(path))


class DeferredVAD:
    """Deterministic queue seam: only EOF reveals the configured speech span."""

    def __init__(self, start=0, length=None):
        self.start, self.length = start, length
        self.windows, self.queue = [], []
        self.flushed = False

    def accept_waveform(self, samples):
        assert len(samples) == 512
        self.windows.append(samples.copy())

    def empty(self):
        return not self.queue

    @property
    def front(self):
        return self.queue[0]

    def pop(self):
        # Native queue views may be invalidated on pop; consumers need a copy.
        self.queue.pop(0).samples[:] = 0

    def flush(self):
        self.flushed = True
        samples = np.concatenate(self.windows) if self.windows else np.empty(0, dtype=np.float32)
        end = self.start + self.length if self.length is not None else len(samples)
        self.queue.append(SimpleNamespace(start=self.start, samples=samples[self.start:end]))


def test_vad_flush_preserves_silence_offset_and_clips_final_padding(tmp_path):
    path = tmp_path / "audio.wav"
    write_pcm(path, np.concatenate((np.zeros(16000), np.full(16017, 8000))))
    vad = DeferredVAD(start=16000)
    chunks = list(speech_chunks(path, vad))
    assert vad.flushed and len(chunks) == 1
    start, samples = chunks[0]
    assert start == 1.0
    assert len(samples) == 16017
    assert np.all(samples == 8000 / 32768)
    assert np.all(np.concatenate(vad.windows)[:16000] == 0)


@pytest.mark.parametrize("seconds", [28.01, 60])
def test_long_vad_segment_is_bounded_without_loss_or_tiny_tail(tmp_path, seconds):
    path = tmp_path / "audio.wav"
    count = int(seconds * ASR_SAMPLE_RATE)
    write_pcm(path, np.full(count, 8000))
    chunks = list(speech_chunks(path, DeferredVAD()))
    assert sum(len(samples) for _, samples in chunks) == count
    assert all(ASR_SAMPLE_RATE <= len(samples) <= ASR_MAX_SECONDS * ASR_SAMPLE_RATE for _, samples in chunks)
    cursor = 0
    for start, samples in chunks:
        assert start == cursor / ASR_SAMPLE_RATE
        cursor += len(samples)


def test_native_whisper_timestamps_use_recording_offset_and_no_word_invention():
    result = SimpleNamespace(
        text="first second", segment_texts=[" first", " second "],
        segment_timestamps=[0.2, 1.5], segment_durations=[0.8, 10.0],
    )
    segments = whisper_segments(result, 30.0, 2.0, "SPEAKER_00")
    assert segments == [
        {"start": 30.2, "end": 31.0, "speaker": "SPEAKER_00", "text": "first"},
        {"start": 31.5, "end": 32.0, "speaker": "SPEAKER_00", "text": "second"},
    ]
    result.segment_texts, result.segment_timestamps, result.segment_durations = [], [], []
    assert whisper_segments(result, 5, 2, "SELF") == [
        {"start": 5, "end": 7, "speaker": "SELF", "text": "first second"}
    ]


def test_native_whisper_rejects_invalid_timestamps():
    result = SimpleNamespace(text="text", segment_texts=["text"], segment_timestamps=[], segment_durations=[1])
    with pytest.raises(PipelineError, match="時刻情報"):
        whisper_segments(result, 0, 1, "SELF")
    result.segment_timestamps = [float("nan")]
    with pytest.raises(PipelineError, match="時刻情報"):
        whisper_segments(result, 0, 1, "SELF")


def test_native_segments_preserve_text_boundaries_and_temporal_speaker_assignment():
    segments = [
        {"start": 1, "end": 2, "speaker": "SPEAKER_00", "text": "hello"},
        {"start": 2, "end": 3, "speaker": "SPEAKER_00", "text": "world"},
        {"start": 3, "end": 5, "speaker": "SPEAKER_00", "text": "next speaker"},
    ]
    turns = [
        {"start": 0, "end": 3.1, "speaker": "SPEAKER_02"},
        {"start": 3.1, "end": 5, "speaker": "SPEAKER_03"},
    ]
    result = assign_speakers(segments, turns)
    assert [item["text"] for item in result] == ["hello", "world", "next speaker"]
    assert [item["speaker"] for item in result] == ["SPEAKER_02", "SPEAKER_02", "SPEAKER_03"]
    assert [(item["start"], item["end"]) for item in result] == [(1, 2), (2, 3), (3, 5)]


def test_transcribe_uses_local_onnx_and_vad_with_independent_track_timelines(tmp_path, monkeypatch):
    write_pcm(tmp_path / "microphone.wav", np.concatenate((np.zeros(16000), np.full(8000, 8000))))
    write_pcm(tmp_path / "system.wav", np.concatenate((np.zeros(32000), np.full(8000, 8000))))
    created_vads, decoded, configs = [], [], []

    class SpeechVAD(DeferredVAD):
        def __init__(self, config, **kwargs):
            super().__init__()
            created_vads.append(self)

        def flush(self):
            samples = np.concatenate(self.windows)
            voiced = np.flatnonzero(samples)
            if len(voiced):
                self.queue.append(SimpleNamespace(start=int(voiced[0]), samples=samples[voiced[0]:voiced[-1] + 1]))

    class Stream:
        def accept_waveform(self, rate, samples):
            assert rate == 16000
            self.samples = samples

    class Recognizer:
        @classmethod
        def from_whisper(cls, **kwargs):
            configs.append(kwargs)
            return cls()

        def create_stream(self):
            return Stream()

        def decode_stream(self, stream):
            decoded.append(stream.samples.copy())
            stream.result = SimpleNamespace(text="speech", segment_texts=["speech"],
                                            segment_timestamps=[0], segment_durations=[0.5])

    fake = SimpleNamespace(
        OfflineRecognizer=Recognizer, VoiceActivityDetector=SpeechVAD,
        SileroVadModelConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        VadModelConfig=lambda **kwargs: SimpleNamespace(validate=lambda: True, **kwargs),
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    models = SimpleNamespace(whisper_path=lambda settings: tmp_path / "model")
    output = Pipeline(models, Secrets())._transcribe(tmp_path, {"language": "ja"})
    assert len(created_vads) == 2 and len(decoded) == 2
    assert output == [
        {"start": 1.0, "end": 1.5, "speaker": "SELF", "text": "speech"},
        {"start": 2.0, "end": 2.5, "speaker": "SPEAKER_00", "text": "speech"},
    ]
    config = configs[0]
    assert config["encoder"] == str(tmp_path / "model" / "encoder.int8.onnx")
    assert config["decoder"] == str(tmp_path / "model" / "decoder.int8.onnx")
    assert config["enable_segment_timestamps"] is True
    assert config.get("enable_token_timestamps", False) is False
    assert config["provider"] == "cpu" and config["language"] == "ja"
    # Digital silence must never reach Whisper, which can hallucinate on it.
    write_pcm(tmp_path / "microphone.wav", np.zeros(16000))
    write_pcm(tmp_path / "system.wav", np.zeros(16000))
    assert Pipeline(models, Secrets())._transcribe(tmp_path, {}) == []
    assert len(decoded) == 2
