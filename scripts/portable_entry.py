"""Frozen entry point. The self-test exercises the exact packaged dependencies."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import wave


def portable_self_test() -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        raise RuntimeError("Run the portable self-test with the frozen Windows executable.")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    # Import native integration libraries without needing Outlook, Teams or a
    # microphone on the hosted runner. Actual device/calendar checks are manual.
    import av
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
    from faster_whisper import WhisperModel

    from giji.models import ModelManager
    from giji.pipeline import Pipeline

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
        manager = ModelManager(folder)
        settings = {"whisper_model": "small", "diarization": True, "language": "ja"}
        assert manager.status(settings)["ready"], manager.status(settings)
        assert manager.whisper_path(settings).parent == Path(sys.executable).parent / "models"

        # Decode PCM through the packaged PyAV DLLs, run both Whisper's encoder
        # and decoder (VAD disabled), then exercise the packaged VAD model too.
        audio = folder / "system.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000 * 12)
        with av.open(str(audio)) as container:
            assert next(container.decode(audio=0)).samples > 0
        model = WhisperModel(
            str(manager.whisper_path(settings)), device="cpu", compute_type="int8",
            cpu_threads=2, local_files_only=True,
        )
        segments, _ = model.transcribe(
            str(audio), language="ja", beam_size=1, vad_filter=False,
            condition_on_previous_text=False,
        )
        list(segments)
        segments, _ = model.transcribe(str(audio), language="ja", vad_filter=True)
        list(segments)
        del model

        # Uses the same path as application diarization, with the real bundled
        # segmentation and embedding ONNX files. No accuracy claim from silence.
        turns = Pipeline(manager, None)._diarize(audio)
        assert isinstance(turns, list)
        assert sherpa_onnx.__version__
        assert not (folder / "models").exists(), "The self-test must not download models."

    print(json.dumps({
        "portable_self_test": "passed",
        "whisper": "small / CPU int8 / local files",
        "diarization": "bundled ONNX / CPU",
        "integrations": "native imports + UI Automation type library",
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
