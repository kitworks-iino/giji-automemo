"""Download public models into a disposable builder cache, then copy complete files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from giji.models import DIARIZATION_REQUIRED_FILES, ModelManager, WHISPER_REQUIRED_FILES


def copy_selected_models(manager: ModelManager, settings: dict, destination: Path) -> None:
    """Copy only the selected ONNX runtime inputs, never an entire old cache."""
    selected = [
        (manager.whisper_path(settings), (*WHISPER_REQUIRED_FILES, "sources.json")),
    ]
    if settings.get("diarization", True):
        selected.append((manager.segmentation_path.parent, (
            *DIARIZATION_REQUIRED_FILES, "sources.json", "segmentation-LICENSE", "segmentation-README.md",
        )))
    for directory, filenames in selected:
        target = destination / directory.name
        target.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            source = directory / name
            if source.is_file() and not source.is_symlink():
                shutil.copy2(source, target / name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    manager = ModelManager(args.cache)
    settings = {"whisper_model": "small", "diarization": True}
    manager.download(settings, print)
    if not manager.status(settings)["ready"]:
        raise RuntimeError("The public model download is incomplete.")
    destination = args.destination / "models"
    if destination.exists():
        shutil.rmtree(destination)
    copy_selected_models(manager, settings, destination)
    manifest = []
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            manifest.append({
                "path": path.relative_to(args.destination).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest,
            })
    (args.destination / "MODEL-MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    assert ModelManager(args.cache / "empty", bundled_models=destination).status(settings)["ready"]


if __name__ == "__main__":
    main()
