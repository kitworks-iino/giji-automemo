import sys

import pytest

from scripts.package_portable import main


@pytest.mark.parametrize("private_name", [
    "teams-connection-journal.json",
    "teams-connection-journal.json.ready",
    "teams-connection-ready-0123456789abcdef0123456789abcdef.json",
])
def test_portable_archive_rejects_local_teams_recovery_state(tmp_path, monkeypatch, private_name):
    distribution = tmp_path / "GijiAutoMemo"
    for relative in (
        "GijiAutoMemo.exe", "_internal/frontend/dist/index.html",
        "models/whisper-small/model.bin", "models/whisper-small/.complete",
        "models/diarization/segmentation.onnx", "models/diarization/embedding.onnx",
        "licenses/README.txt", "third-party-sources/SOURCES.json", private_name,
    ):
        path = distribution / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test fixture", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "package_portable.py", "--directory", str(distribution),
        "--output", str(tmp_path / "public.zip"),
    ])
    with pytest.raises(RuntimeError, match="Unexpected private runtime file"):
        main()
