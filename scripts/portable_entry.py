"""Frozen entry point. The self-test exercises the exact packaged dependencies."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import struct
import sysconfig
import shutil
import tempfile
import wave


def teams_runtime_self_test(folder: Path) -> None:
    """Exercise packaged Teams dependencies without enabling or probing CDP."""
    import websocket
    import win32api
    import win32con
    import win32gui
    import win32security
    from tzlocal import get_localzone_name
    from zoneinfo import ZoneInfo

    from giji.teams_calendar import TeamsDesktopCalendar
    from giji.teams_calendar_parser import parse_calendar_snapshot
    from giji.teams_calendar_uia import TeamsUIACalendar
    from giji.teams_connection import (
        JOURNAL_NAME,
        TeamsConnectionManager,
        _WindowsBackend,
        cleanup_main,
        validate_cdp_url,
    )

    assert websocket.create_connection and websocket.__version__
    assert win32security.GetNamedSecurityInfo and win32api.GetCurrentProcess
    assert win32con.TOKEN_QUERY
    desktop = win32gui.GetDesktopWindow()
    assert desktop and win32gui.IsWindow(desktop)
    # Read-only native calls used to identify background windows. This does
    # not enumerate, foreground, restore, or interact with a Teams window.
    assert isinstance(win32gui.IsIconic(desktop), int)
    assert isinstance(win32gui.IsWindowVisible(desktop), int)
    assert callable(cleanup_main), "The detached cleanup helper must be included."
    assert TeamsDesktopCalendar
    # Windows local-zone mapping and IANA database are both included. Merely
    # reading the configured zone does not change Windows or Teams settings.
    assert ZoneInfo(get_localzone_name())
    assert TeamsUIACalendar
    assert validate_cdp_url("http://127.0.0.1:49152/json/list", "http://127.0.0.1:49152")
    assert not validate_cdp_url("http://example.com:49152/json/list", "http://127.0.0.1:49152")

    # Exercise the native process/SID APIs used to establish Teams ownership on
    # a harmless process we own. Hosted CI has no installed Teams package, so
    # its package ancestry is covered separately by synthetic ownership tests.
    backend = _WindowsBackend()
    current = backend._process(os.getpid())
    assert current.pid == os.getpid() and current.owner == backend.owner()
    assert Path(current.executable).resolve() == Path(sys.executable).resolve()
    reader = TeamsUIACalendar()
    assert reader.status()["state"] == "not_synced"
    reader.close()

    # A fresh temporary directory has no recovery journal. Constructor, status,
    # and close therefore perform no policy writes, no launch, and no network.
    data_dir = folder / "teams-neutral"
    manager = TeamsConnectionManager(data_dir)
    neutral = manager.status()
    assert neutral["state"] == "needs_setup" and not neutral["connected"]
    assert neutral["policy_restored"] and not neutral["debug_active"]
    closed = manager.close()
    assert not closed["connected"] and closed["policy_restored"]
    assert not (data_dir / JOURNAL_NAME).exists()

    # Synthetic visible text tests the bundled parser/timezone database only;
    # it makes no claim about compatibility with a particular live Teams build.
    parsed = parse_calendar_snapshot({
        "timezone": "Asia/Tokyo", "date_texts": ["2026年10月21日"],
        "day_view": True, "empty_text": "", "events": [
            {"title": "Synthetic smoke", "label": "10:00から11:00"},
        ],
    }, now=datetime(2026, 10, 21, 3, tzinfo=timezone.utc))
    assert parsed["meetings"][0]["start"] == "2026-10-21T01:00:00+00:00"
    assert parsed["coverage"] == "visible_only"


def portable_self_test() -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        raise RuntimeError("Run the portable self-test with the frozen Windows executable.")
    assert sys.version_info[:2] == (3, 13), "The portable runtime must be Python 3.13."
    assert (struct.calcsize("P") == 8 and sysconfig.get_platform() == "win-amd64"
            and not sysconfig.get_config_var("Py_GIL_DISABLED")), (
        "The portable runtime must be standard x64 Python."
    )
    # Import native integration libraries without needing Outlook, Teams or a
    # microphone on the hosted runner. Actual device/calendar checks are manual.
    import comtypes.client
    import ctypes
    import keyring.backends.Windows
    import numpy as np
    import pyaudiowpatch
    import pythoncom
    import pywinauto
    import scipy.signal
    import sherpa_onnx
    import win32com.client

    from giji.models import ModelManager
    from giji.pipeline import Pipeline
    from giji.runtime_dependencies import ensure_no_prohibited_dependencies

    ensure_no_prohibited_dependencies()

    # Windows credential backend and UI Automation typelib must be importable.
    keyring.backends.Windows.WinVaultKeyring()
    comtypes.client.GetModule("UIAutomationCore.dll")
    assert pyaudiowpatch.paInt16 and pywinauto.__version__
    assert pythoncom.COINIT_APARTMENTTHREADED is not None
    assert win32com.client.Dispatch
    assert scipy.signal.resample_poly(np.zeros(16, dtype=np.float32), 2, 1).size == 32
    # A developer/CI host has global VC runtimes installed. Verify the process
    # loaded the copies shipped with the app rather than passing accidentally.
    for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        library = ctypes.WinDLL(name)
        buffer = ctypes.create_unicode_buffer(32768)
        get_filename = ctypes.windll.kernel32.GetModuleFileNameW
        get_filename.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        get_filename.restype = ctypes.c_uint
        assert get_filename(library._handle, buffer, len(buffer))
        assert Path(buffer.value).resolve().is_relative_to(Path(sys.executable).parent), (
            f"The packaged app loaded {name} from outside its own directory."
        )

    with tempfile.TemporaryDirectory(prefix="giji-portable-test-") as temporary:
        folder = Path(temporary)
        teams_runtime_self_test(folder)
        manager = ModelManager(folder)
        settings = {"whisper_model": "small", "diarization": True, "language": "ja"}
        assert manager.status(settings)["ready"], manager.status(settings)
        assert manager.whisper_path(settings).parent == Path(sys.executable).parent / "models"

        # Decode only the app's PCM WAV format through the standard library.
        # Direct decoding exercises both ONNX encoder and decoder without VAD;
        # the production transcription path separately exercises Silero VAD.
        audio = folder / "system.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000 * 12)
        shutil.copyfile(audio, folder / "microphone.wav")
        with wave.open(str(audio), "rb") as source:
            samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32)
            samples /= 32768.0
        model_path = manager.whisper_path(settings)
        recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(model_path / "encoder.int8.onnx"),
            decoder=str(model_path / "decoder.int8.onnx"), tokens=str(model_path / "tokens.txt"),
            num_threads=2, language="ja", task="transcribe", provider="cpu",
            enable_segment_timestamps=True,
        )
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        recognizer.decode_stream(stream)
        assert isinstance(stream.result.text, str)
        del stream, recognizer
        assert Pipeline(manager, None)._transcribe(folder, settings) == []

        # Uses the same path as application diarization, with the real bundled
        # segmentation and embedding ONNX files. No accuracy claim from silence.
        turns = Pipeline(manager, None)._diarize(audio)
        assert isinstance(turns, list)
        assert sherpa_onnx.__version__
        assert not (folder / "models").exists(), "The self-test must not download models."

    print(json.dumps({
        "portable_self_test": "passed",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} / x64",
        "whisper": "sherpa-onnx small / CPU int8 / stdlib PCM WAV / local files",
        "diarization": "bundled ONNX / CPU",
        "integrations": "native imports + UI Automation + Teams setup-neutral status/parser/cleanup imports",
    }))


if __name__ == "__main__":
    # PyInstaller's multiprocessing support must run before parsing application
    # arguments. It also prevents recursive app launches from native packages.
    from multiprocessing import freeze_support
    freeze_support()
    if sys.argv[1:] == ["--portable-self-test"]:
        portable_self_test()
    else:
        from giji.__main__ import main
        main()
