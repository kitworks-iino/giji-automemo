"""Windows WASAPI recording, with independent microphone and output tracks.

The output track is the mix of the selected playback device, not just Teams.
No audio leaves this module or the computer. PyAudioWPatch is imported lazily,
so the rest of the application and its tests also run on other platforms.
"""

from __future__ import annotations

import array
import importlib
import json
import math
import os
import platform
import queue
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AudioError(RuntimeError):
    """A user-displayable error which never contains recorded content."""


def _load_audio() -> Any:
    if platform.system() != "Windows":
        raise AudioError("録音は Windows 11 で利用できます。")
    try:
        return importlib.import_module("pyaudiowpatch")
    except ImportError as exc:
        raise AudioError(
            "録音機能が未インストールです。Windows 用のセットアップを実行してください。"
        ) from exc


def _level(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    # A square-root scale makes speech visible without declaring silence a failure.
    return min(1.0, math.sqrt(rms / 32768.0))


class _TrackWriter:
    """Write a PCM16 timeline, including silence when loopback callbacks pause.

    Headers are patched at every append, and file buffers are fsynced once per
    second. A forcibly terminated recording can therefore usually be played
    through its last flushed block instead of having an empty WAV header.
    """

    def __init__(self, path: Path, sample_rate: int, channels: int) -> None:
        self.rate = sample_rate
        self.channels = channels
        self.frame_size = channels * 2
        self.frames = 0
        self._file = path.open("xb")
        self._wave = wave.open(self._file, "wb")
        self._wave.setnchannels(channels)
        self._wave.setsampwidth(2)
        self._wave.setframerate(sample_rate)
        self._wave.writeframes(b"")
        self._last_sync = time.monotonic()

    def _write(self, pcm: bytes) -> None:
        # RIFF/WAV has a 4 GiB limit. Fail explicitly rather than corrupt a file.
        if (self.frames * self.frame_size + len(pcm)) > 0xFFFFFF00:
            raise AudioError("録音ファイルが WAV の上限に達しました。録音を停止してください。")
        self._wave.writeframes(pcm)
        self.frames += len(pcm) // self.frame_size
        if time.monotonic() - self._last_sync >= 1:
            self.flush()

    def pad_to(self, seconds: float) -> None:
        target = max(0, round(seconds * self.rate))
        # Write at most a second at a time; a long silence never allocates a huge buffer.
        while self.frames < target:
            count = min(self.rate, target - self.frames)
            self._write(b"\0" * (count * self.frame_size))

    def append(self, pcm: bytes, start_seconds: float) -> None:
        if len(pcm) % self.frame_size:
            raise AudioError("音声データの形式を読み取れませんでした。")
        target = max(0, round(start_seconds * self.rate))
        if target > self.frames:
            self.pad_to(start_seconds)
        elif target < self.frames:
            # Overlap can occur if a late callback follows a filled silent gap.
            pcm = pcm[min(len(pcm), (self.frames - target) * self.frame_size) :]
        if pcm:
            self._write(pcm)

    def flush(self) -> None:
        self._file.flush()
        os.fsync(self._file.fileno())
        self._last_sync = time.monotonic()

    def close(self) -> None:
        try:
            self._wave.close()
            self.flush()
        finally:
            self._file.close()


class AudioRecorder:
    def __init__(self) -> None:
        self._lifecycle = threading.Lock()
        self._audio: Any = None
        self._streams: dict[str, Any] = {}
        self._tracks: dict[str, _TrackWriter] = {}
        self._queue: queue.Queue[tuple[str, float, bytes]] = queue.Queue(maxsize=2048)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._started = 0.0
        self._duration = 0.0
        self._running = False
        self._error: str | None = None
        self._levels = {"microphone": 0.0, "system": 0.0}
        self._last_callback = {"microphone": 0.0, "system": 0.0}

    @staticmethod
    def list_devices() -> dict[str, list[dict[str, Any]]]:
        if platform.system() != "Windows":
            return {"microphones": [], "loopbacks": []}
        module = _load_audio()
        audio = module.PyAudio()
        try:
            wasapi = audio.get_host_api_info_by_type(module.paWASAPI)
            host_id = int(wasapi["index"])
            microphones = []
            loopbacks = []
            for index in range(audio.get_device_count()):
                device = audio.get_device_info_by_index(index)
                if int(device.get("hostApi", -1)) != host_id:
                    continue
                if int(device.get("maxInputChannels", 0)) <= 0:
                    continue
                item = {"id": int(device["index"]), "name": str(device["name"])}
                (loopbacks if device.get("isLoopbackDevice") else microphones).append(item)
            return {"microphones": microphones, "loopbacks": loopbacks}
        except Exception as exc:
            raise AudioError("音声デバイスを取得できません。Windows のサウンド設定を確認してください。") from exc
        finally:
            audio.terminate()

    @staticmethod
    def _devices(audio: Any, module: Any, settings: dict) -> dict[str, dict]:
        wasapi = audio.get_host_api_info_by_type(module.paWASAPI)
        mic_id = settings.get("microphone_id")
        if mic_id is None:
            mic_id = wasapi["defaultInputDevice"]
        if int(mic_id) < 0:
            raise AudioError("マイクが見つかりません。接続と Windows のマイク権限を確認してください。")
        mic = audio.get_device_info_by_index(int(mic_id))
        output_id = settings.get("loopback_id")
        output = (
            audio.get_default_wasapi_loopback()
            if output_id is None
            else audio.get_device_info_by_index(int(output_id))
        )
        if mic.get("isLoopbackDevice") or int(mic.get("maxInputChannels", 0)) < 1:
            raise AudioError("選択されたマイクを利用できません。設定で選び直してください。")
        if not output.get("isLoopbackDevice") or int(output.get("maxInputChannels", 0)) < 1:
            raise AudioError("選択された再生デバイスを録音できません。設定で選び直してください。")
        for device in (mic, output):
            if int(device["hostApi"]) != int(wasapi["index"]):
                raise AudioError("WASAPI デバイスを選択してください。")
        return {"microphone": mic, "system": output}

    def _callback(self, name: str, sample_rate: int, module: Any):
        adc_anchor: float | None = None
        elapsed_anchor = 0.0
        next_sample = 0.0

        def receive(data: bytes, frame_count: int, timing: dict, flags: int):
            nonlocal adc_anchor, elapsed_anchor, next_sample
            now = time.monotonic()
            elapsed = max(0.0, now - self._started)
            self._last_callback[name] = now
            if self._stop.is_set() or self._error:
                return (None, module.paAbort)
            if flags & module.paInputOverflow:
                self._error = "音声処理が追いつかず録音が欠落しました。保存済みの音声は残っています。"
                return (None, module.paAbort)
            adc = float(timing.get("input_buffer_adc_time", 0.0))
            current = float(timing.get("current_time", 0.0))
            block_seconds = frame_count / sample_rate
            if adc_anchor is None:
                latency = current - adc
                if adc <= 0 or current <= 0 or not (0 <= latency < 5):
                    latency = block_seconds
                elapsed_anchor = max(0.0, elapsed - latency)
                adc_anchor = adc
                offset = elapsed_anchor
            elif adc > adc_anchor and adc > 0:
                # PortAudio clock tracks gaps while the render endpoint is silent.
                offset = elapsed_anchor + (adc - adc_anchor)
            else:
                # Some audio drivers do not supply a capture timestamp.
                # Keep sequence continuity, but retain a real gap in callbacks.
                offset = max(next_sample, elapsed - block_seconds)
            if offset > elapsed + 2:
                self._error = "音声デバイスの時刻が変化しました。録音を停止してデバイスを確認してください。"
                return (None, module.paAbort)
            next_sample = offset + block_seconds
            try:
                self._queue.put_nowait((name, offset, data or b""))
            except queue.Full:
                self._error = "録音の保存が追いつきません。空き容量とディスクの状態を確認してください。"
                return (None, module.paAbort)
            return (None, module.paContinue)

        return receive

    def start(self, directory: Path, settings: dict) -> None:
        with self._lifecycle:
            if self._running:
                raise AudioError("すでに録音中です。")
            if self._worker and self._worker.is_alive():
                raise AudioError("前の録音を保存中です。完了を待ってください。")
            if not settings.get("recording_consent"):
                raise AudioError("録音前に、設定で参加者への案内・同意を確認してください。")
            module = _load_audio()
            self._error = None
            self._duration = 0.0
            self._levels = {"microphone": 0.0, "system": 0.0}
            self._last_callback = {"microphone": 0.0, "system": 0.0}
            self._worker = None
            self._stop.clear()
            self._queue = queue.Queue(maxsize=2048)
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            self._audio = module.PyAudio()
            try:
                devices = self._devices(self._audio, module, settings)
                descriptions = {}
                for name, device in devices.items():
                    channels = int(device["maxInputChannels"])
                    sample_rate = int(device["defaultSampleRate"])
                    self._tracks[name] = _TrackWriter(
                        directory / f"{name}.wav", sample_rate, channels
                    )
                    descriptions[name] = {
                        "id": int(device["index"]),
                        "name": str(device["name"]),
                        "sample_rate": sample_rate,
                        "channels": channels,
                    }
                    self._streams[name] = self._audio.open(
                        format=module.paInt16,
                        channels=channels,
                        rate=sample_rate,
                        input=True,
                        input_device_index=int(device["index"]),
                        frames_per_buffer=1024,
                        start=False,
                        stream_callback=self._callback(name, sample_rate, module),
                    )
                (directory / "capture.json").write_text(
                    json.dumps(
                        {
                            "started_at": datetime.now(timezone.utc).isoformat(),
                            "tracks": descriptions,
                            "output_scope": "selected_playback_device_mix",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                self._started = time.monotonic()
                self._worker = threading.Thread(
                    target=self._write_loop, name="giji-audio-writer", daemon=True
                )
                self._worker.start()
                for stream in self._streams.values():
                    stream.start_stream()
                self._running = True
            except Exception as exc:
                self._duration = 0
                self._stop.set()
                self._close_streams()
                if self._worker:
                    self._worker.join(timeout=5)
                else:
                    self._close_tracks()
                self._running = False
                if isinstance(exc, AudioError):
                    raise
                raise AudioError(
                    "録音を開始できません。マイク権限と、Teams と同じ再生デバイスが選ばれているか確認してください。"
                ) from exc

    def _write_loop(self) -> None:
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    name, offset, pcm = self._queue.get(timeout=0.1)
                    self._tracks[name].append(pcm, offset)
                    self._levels[name] = _level(pcm)
                except queue.Empty:
                    # Delay gap fill to tolerate callback scheduling jitter.
                    until = max(0.0, time.monotonic() - self._started - 2)
                    for track in self._tracks.values():
                        track.pad_to(until)
                if self._error:
                    break
            for track in self._tracks.values():
                track.pad_to(self._duration)
        except Exception:
            self._error = self._error or (
                "音声を保存できませんでした。空き容量を確認してください。保存済みの音声は残っています。"
            )
        finally:
            self._close_tracks()

    def _close_tracks(self) -> None:
        for track in self._tracks.values():
            try:
                track.close()
            except Exception:
                self._error = self._error or "音声ファイルの保存時にエラーが発生しました。"
        self._tracks = {}

    def _close_streams(self) -> None:
        for stream in self._streams.values():
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self._streams = {}
        if self._audio is not None:
            try:
                self._audio.terminate()
            except Exception:
                self._error = self._error or "音声デバイスを閉じる処理でエラーが発生しました。"
            finally:
                self._audio = None

    def stop(self) -> dict[str, float]:
        with self._lifecycle:
            if not self._running:
                return {"duration_seconds": round(self._duration, 3)}
            self._duration = max(0.0, time.monotonic() - self._started)
            # UI status polling may run concurrently with native stream close.
            # Do not interpret this intentional stop as a device disconnect.
            self._running = False
            self._close_streams()
            self._stop.set()
            if self._worker:
                self._worker.join(timeout=10)
                if self._worker.is_alive():
                    self._error = "録音の保存処理が応答していません。アプリを終了せずにお待ちください。"
                    raise AudioError(self._error)
            self._worker = None
            self._running = False
            return {"duration_seconds": round(self._duration, 3)}

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        seconds = max(0.0, now - self._started) if self._running else self._duration
        if self._running and not self._error:
            if seconds > 10 and now - self._last_callback["microphone"] > 10:
                self._error = "マイクから音声が届いていません。接続と Windows のマイク権限を確認してください。"
            for stream in list(self._streams.values()):
                try:
                    if not stream.is_active() and self._running:
                        self._error = "音声デバイスが停止しました。接続を確認してください。録音済みの音声は保存されています。"
                except Exception:
                    if self._running:
                        self._error = "音声デバイスとの接続が失われました。"
        levels = {
            name: level if now - self._last_callback[name] < 0.5 else 0.0
            for name, level in self._levels.items()
        }
        return {
            "seconds": round(seconds, 3),
            "microphone_level": round(levels["microphone"], 3),
            "loopback_level": round(levels["system"], 3),
            "error": self._error,
        }
