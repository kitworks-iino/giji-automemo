# Build on Windows x64 with Python 3.13. Models live beside the executable;
# frontend assets remain relative to giji/api.py inside PyInstaller's _internal.
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_delvewheel_libs_directory,
    collect_submodules,
    copy_metadata,
)

root = Path(SPECPATH).parent
datas = [(str(root / "frontend" / "dist"), "frontend/dist")]
binaries = []
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "keyring.backends.Windows",
    "win32ctypes.pywin32.win32cred",
    "win32com.client",
    "win32com.server",
    "win32timezone",
    "win32security",
    "win32api",
    "win32con",
    "win32gui",
    "pythoncom",
    "pywintypes",
    "comtypes.gen.UIAutomationClient",
    # The recovery watchdog is launched through a CLI switch in this same exe.
    "giji.teams_connection",
    "giji.teams_calendar",
    "giji.teams_calendar_parser",
    "giji.teams_calendar_uia",
]

# These packages load native extension modules, model data, DLLs or backends
# dynamically. Explicit collection complements PyInstaller's standard hooks.
for package in (
    "faster_whisper", "ctranslate2", "sherpa_onnx", "av", "onnxruntime",
    "tokenizers", "pyaudiowpatch", "pywinauto", "comtypes", "tzdata", "tzlocal", "websocket",
):
    package_datas, package_binaries, package_hidden = collect_all(
        package,
        filter_submodules=lambda name: not any(
            part in name.split(".") for part in ("tests", "test", "converters")
        ),
    )
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hidden)

for package in ("scipy", "numpy", "av", "onnxruntime", "sherpa_onnx", "ctranslate2"):
    datas, binaries = collect_delvewheel_libs_directory(
        package, datas=datas, binaries=binaries,
    )

hiddenimports.extend(collect_submodules("comtypes.gen"))
for distribution in ("keyring", "faster-whisper", "sherpa-onnx", "websocket-client", "giji-automemo"):
    datas.extend(copy_metadata(distribution))

a = Analysis(
    [str(root / "scripts" / "portable_entry.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "torch", "tensorflow", "transformers", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="GijiAutoMemo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    contents_directory="_internal",
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False,
    upx=False,
    name="GijiAutoMemo",
)
