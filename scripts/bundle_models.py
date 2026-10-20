"""Download public models into a disposable builder cache, then copy complete files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from giji.models import ModelManager


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
    shutil.copytree(manager.directory, destination, ignore=shutil.ignore_patterns(".cache", "*.lock"))
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
