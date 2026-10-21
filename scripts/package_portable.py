"""Create and validate the public ZIP; user data is never read by this script."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.audit_dependencies import audit_archive, audit_directory  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    required = [
        "GijiAutoMemo.exe", "_internal/frontend/dist/index.html",
        "models/whisper-onnx-small/encoder.int8.onnx", "models/whisper-onnx-small/decoder.int8.onnx",
        "models/whisper-onnx-small/tokens.txt", "models/whisper-onnx-small/silero_vad.onnx",
        "models/whisper-onnx-small/.complete",
        "models/diarization/segmentation.onnx", "models/diarization/embedding.onnx",
        "licenses/README.txt", "third-party-sources/SOURCES.json", "DEPENDENCY-ENVIRONMENT.json",
    ]
    for name in required:
        if not (args.directory / name).is_file():
            raise RuntimeError(f"The portable distribution is incomplete: {name}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = audit_directory(args.directory)
    (args.directory / "DEPENDENCY-AUDIT.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(args.directory.rglob("*")):
            if path.is_file():
                if (
                    path.name in {"launch-url.txt", ".instance.lock", "teams-connection-journal.json"}
                    or path.name.startswith("teams-connection-journal.json.")
                    or path.name.startswith("teams-connection-ready-")
                    or path.suffix in {".wav", ".sqlite3"}
                ):
                    raise RuntimeError(f"Unexpected private runtime file in the distribution: {path.name}")
                archive.write(path, "GijiAutoMemo/" + path.relative_to(args.directory).as_posix())
    if args.output.stat().st_size >= 2 * 1024**3:
        raise RuntimeError("The portable ZIP exceeds the release asset limit of 2 GiB.")
    with zipfile.ZipFile(args.output) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), "Duplicate archive paths."
        assert archive.testzip() is None, "The ZIP integrity check failed."
    audit_archive(args.output)
    with args.output.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    (args.output.parent / "portable-SHA256SUMS.txt").write_text(
        f"{digest}  {args.output.name}\n", encoding="ascii",
    )
    print(f"Verified {args.output.name}: {args.output.stat().st_size:,} bytes, SHA-256 {digest}")


if __name__ == "__main__":
    main()
