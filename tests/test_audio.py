import array
import wave

import pytest

from giji.audio import AudioError, AudioRecorder, _level, _TrackWriter


def test_track_writer_preserves_silent_gap_and_separate_timeline(tmp_path):
    output = tmp_path / "track.wav"
    track = _TrackWriter(output, 1000, 1)
    track.append(array.array("h", [1000] * 100).tobytes(), 0.25)
    track.append(array.array("h", [2000] * 100).tobytes(), 1)
    track.pad_to(1.5)
    track.close()
    with wave.open(str(output), "rb") as reader:
        assert reader.getnframes() == 1500
        samples = array.array("h", reader.readframes(1500))
    assert all(x == 0 for x in samples[:250])
    assert all(x == 1000 for x in samples[250:350])
    assert all(x == 0 for x in samples[350:1000])
    assert all(x == 2000 for x in samples[1000:1100])
    assert all(x == 0 for x in samples[1100:])


def test_late_packet_does_not_duplicate_audio(tmp_path):
    track = _TrackWriter(tmp_path / "track.wav", 1000, 1)
    track.append(array.array("h", [100] * 100).tobytes(), 0)
    track.append(array.array("h", [200] * 100).tobytes(), 0.05)
    assert track.frames == 150
    track.close()


def test_headers_are_valid_before_close(tmp_path):
    output = tmp_path / "track.wav"
    track = _TrackWriter(output, 1000, 2)
    track.append(b"\0" * 400, 0)
    track.flush()
    with wave.open(str(output), "rb") as reader:
        assert reader.getnframes() == 100
        assert reader.getnchannels() == 2
    track.close()


def test_existing_recording_never_overwritten(tmp_path):
    output = tmp_path / "track.wav"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        _TrackWriter(output, 1000, 1)
    assert output.read_bytes() == b"existing"


def test_pcm_levels():
    assert _level(b"\0" * 200) == 0
    assert 0 < _level(array.array("h", [1000, -1000] * 100).tobytes()) < 1
    assert _level(b"") == 0


def test_unsupported_platform_and_consent(monkeypatch, tmp_path):
    monkeypatch.setattr("giji.audio.platform.system", lambda: "Linux")
    assert AudioRecorder.list_devices() == {"microphones": [], "loopbacks": []}
    with pytest.raises(AudioError, match="同意"):
        AudioRecorder().start(tmp_path, {})
    with pytest.raises(AudioError, match="Windows"):
        AudioRecorder().start(tmp_path, {"recording_consent": True})


class FakeModule:
    paAbort = 2
    paContinue = 0
    paInputOverflow = 2
    paWASAPI = 13
    paInt16 = 8


def test_callback_tracks_native_timestamps_including_silence(monkeypatch):
    recorder = AudioRecorder()
    recorder._started = 10
    monkeypatch.setattr("giji.audio.time.monotonic", lambda: 10.12)
    callback = recorder._callback("system", 1000, FakeModule)
    assert callback(b"\0" * 200, 100, {"current_time": 5.12, "input_buffer_adc_time": 5}, 0) == (None, 0)
    first = recorder._queue.get_nowait()
    assert first[1] == pytest.approx(0)
    monkeypatch.setattr("giji.audio.time.monotonic", lambda: 13.12)
    callback(b"\0" * 200, 100, {"current_time": 8.12, "input_buffer_adc_time": 8}, 0)
    second = recorder._queue.get_nowait()
    assert second[1] == pytest.approx(3)


def test_overflow_is_an_explicit_error():
    recorder = AudioRecorder()
    callback = recorder._callback("microphone", 48000, FakeModule)
    assert callback(b"", 1024, {}, FakeModule.paInputOverflow)[1] == FakeModule.paAbort
    assert "欠落" in recorder.status()["error"]


def test_devices_reject_wrong_track():
    class Audio:
        def get_host_api_info_by_type(self, _):
            return {"defaultInputDevice": 0, "index": 2}

        def get_device_info_by_index(self, index):
            return {"index": index, "maxInputChannels": 2, "isLoopbackDevice": True, "hostApi": 2}

        def get_default_wasapi_loopback(self):
            return self.get_device_info_by_index(1)

    with pytest.raises(AudioError, match="マイク"):
        AudioRecorder._devices(Audio(), FakeModule, {})


def test_both_tracks_are_closed_and_playable_after_stop(tmp_path, monkeypatch):
    class Stream:
        def __init__(self, callback):
            self.callback = callback
            self.active = False
            self.closed = False

        def start_stream(self):
            self.active = True
            self.callback(b"\0" * 2048, 1024, {}, 0)

        def stop_stream(self):
            self.active = False

        def close(self):
            self.closed = True

        def is_active(self):
            return self.active

    class Audio:
        def __init__(self):
            self.streams = []
            self.terminated = False

        def get_host_api_info_by_type(self, _):
            return {"defaultInputDevice": 0, "index": 2}

        def get_device_info_by_index(self, index):
            return {
                "index": index, "name": "test device", "maxInputChannels": 1,
                "isLoopbackDevice": bool(index), "hostApi": 2, "defaultSampleRate": 48000,
            }

        def get_default_wasapi_loopback(self):
            return self.get_device_info_by_index(1)

        def open(self, **kwargs):
            stream = Stream(kwargs["stream_callback"])
            self.streams.append(stream)
            return stream

        def terminate(self):
            self.terminated = True

    audio = Audio()
    module = FakeModule()
    module.PyAudio = lambda: audio
    monkeypatch.setattr("giji.audio._load_audio", lambda: module)
    recorder = AudioRecorder()
    recorder.start(tmp_path, {"recording_consent": True})
    result = recorder.stop()
    assert result["duration_seconds"] >= 0
    assert audio.terminated
    assert all(stream.closed for stream in audio.streams)
    for track in ("microphone", "system"):
        with wave.open(str(tmp_path / f"{track}.wav"), "rb") as reader:
            assert reader.getnframes() >= 1024
            assert reader.getframerate() == 48000
    assert recorder.stop() == result


def test_status_poll_during_intentional_stop_is_not_device_failure(monkeypatch):
    recorder = AudioRecorder()
    recorder._running = True
    recorder._started = 10
    monkeypatch.setattr("giji.audio.time.monotonic", lambda: 11)
    class ClosingStream:
        def is_active(self):
            return False
    recorder._streams = {"microphone": ClosingStream()}
    def close_streams():
        assert recorder.status()["error"] is None
        recorder._streams = {}
    monkeypatch.setattr(recorder, "_close_streams", close_streams)
    assert recorder.stop()["duration_seconds"] == 1
    assert recorder.status()["error"] is None
