"""Collect redistribution notices and upstream source material for a portable build.

Run with the same clean virtual environment used by PyInstaller. Downloads are
build-time only. Downloaded Python is parsed as data and is never executed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile, ZIP_DEFLATED

from giji.runtime_dependencies import ensure_no_prohibited_dependencies


ROOT = Path(__file__).resolve().parent.parent
NOTICE_PATTERN = re.compile(r"^(licen[cs]e|copying|copyright|notice|authors)([._-].*)?$", re.I)
SOURCE_EXTENSIONS = {".py", ".pyi", ".pyx", ".pxd", ".pxi", ".h", ".c", ".cpp", ".toml"}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def safe_component(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    if not result or result in {".", ".."}:
        raise ValueError(f"Invalid package component: {value!r}")
    return result


def download(url: str, destination: Path, expected_sha256: str | None = None) -> dict:
    """Only HTTPS; atomic write; optional upstream hash; no authorization headers."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError(f"Unsafe source URL: {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and (expected_sha256 is None or digest(destination) == expected_sha256):
        return {"url": url, "sha256": digest(destination), "bytes": destination.stat().st_size}
    temporary = destination.with_name(destination.name + ".partial")
    for attempt in range(4):
        try:
            request = Request(url, headers={"User-Agent": "giji-automemo-source-collector/0.5"})
            with urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                if urlparse(response.url).scheme != "https":
                    raise RuntimeError(f"Source redirected to an insecure URL: {url}")
                total = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > 750 * 1024 * 1024:
                        raise RuntimeError(f"Unexpectedly large source archive: {url}")
                    output.write(chunk)
            checksum = digest(temporary)
            if expected_sha256 is not None and checksum != expected_sha256:
                raise RuntimeError(f"Source checksum mismatch: {destination.name}")
            if temporary.stat().st_size == 0:
                raise RuntimeError(f"Empty source download: {destination.name}")
            os.replace(temporary, destination)
            return {"url": url, "sha256": checksum, "bytes": destination.stat().st_size}
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("Unreachable")


def is_notice(path: PurePosixPath | Path) -> bool:
    return bool(NOTICE_PATTERN.match(path.name)) or any(
        item.lower() in {"licenses", "licences"} for item in path.parts
    )


def collect_python(licenses: Path, sources: Path) -> list[dict]:
    records: list[dict] = []
    # Only files enumerated by installed distribution metadata are considered.
    # No user profile, credentials, working data or environment files are scanned.
    with ZipFile(sources / "installed-python-source.zip", "w", ZIP_DEFLATED) as archive:
        for dist in sorted(metadata.distributions(), key=lambda d: d.metadata.get("Name", "").lower()):
            name = dist.metadata.get("Name", "unknown")
            folder = safe_component(f"{name}-{dist.version}")
            notices = []
            for item in dist.files or []:
                relative = PurePosixPath(str(item).replace("\\", "/"))
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                path = Path(dist.locate_file(item))
                if not path.is_file() or path.is_symlink():
                    continue
                if is_notice(relative):
                    target = licenses / "python" / folder / Path(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    notices.append(relative.as_posix())
                if path.suffix.lower() in SOURCE_EXTENSIONS or is_notice(relative):
                    archive.write(path, f"{folder}/{relative.as_posix()}")
            record = {
                "name": name,
                "version": dist.version,
                "license": dist.metadata.get("License-Expression") or dist.metadata.get("License"),
                "project_urls": dist.metadata.get_all("Project-URL", []),
                "homepage": dist.metadata.get("Home-page"),
                "notice_files": notices,
            }
            records.append(record)
            # Preserve the full original package metadata, including license text
            # embedded in metadata instead of in a separate license file.
            target = licenses / "python" / folder / "METADATA.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            original_metadata = dist.read_text("METADATA") or dist.read_text("PKG-INFO")
            if original_metadata is None:
                raise RuntimeError(f"Original distribution metadata is missing: {name}")
            target.write_text(original_metadata, encoding="utf-8")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / "LICENSE"
    if not python_license.is_file():
        # CPython installers include LICENSE.txt. Missing it must fail a Windows
        # distribution, rather than silently omitting the interpreter notices.
        if sys.platform == "win32":
            raise RuntimeError("The build Python interpreter's LICENSE.txt was not found")
    else:
        shutil.copy2(python_license, licenses / "PYTHON-LICENSE.txt")
    return records


def collect_frontend(licenses: Path, sources: Path) -> list[dict]:
    frontend = ROOT / "frontend"
    lock = json.loads((frontend / "package-lock.json").read_text(encoding="utf-8"))
    records = []
    with ZipFile(sources / "frontend-installed-packages.zip", "w", ZIP_DEFLATED) as archive:
        for relative, info in sorted(lock["packages"].items()):
            if not relative.startswith("node_modules/") or info.get("dev"):
                continue
            package = frontend / relative
            if not package.is_dir():
                if info.get("optional"):
                    continue
                raise RuntimeError(f"Frontend production dependency is missing: {relative}")
            manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
            folder = safe_component(f"{manifest['name']}-{manifest['version']}")
            notices = []
            for path in package.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                child = path.relative_to(package)
                if "node_modules" in child.parts:
                    continue
                archive.write(path, f"{folder}/{child.as_posix()}")
                if is_notice(child):
                    target = licenses / "frontend" / folder / child
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    notices.append(child.as_posix())
            records.append({
                "name": manifest["name"], "version": manifest["version"],
                "license": manifest.get("license"), "repository": manifest.get("repository"),
                "resolved": info.get("resolved"), "integrity": info.get("integrity"),
                "notice_files": notices,
            })
    return records


def collect_models(destination: Path, licenses: Path) -> list[dict]:
    target = licenses / "models"
    target.mkdir(parents=True, exist_ok=True)
    records = []
    for name, url in {
        "whisper-LICENSE.txt": "https://raw.githubusercontent.com/openai/whisper/main/LICENSE",
        "sherpa-onnx-LICENSE.txt": "https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v1.13.8/LICENSE",
        "silero-vad-LICENSE.txt": "https://raw.githubusercontent.com/snakers4/silero-vad/master/LICENSE",
        "3D-Speaker-LICENSE.txt": "https://raw.githubusercontent.com/modelscope/3D-Speaker/main/LICENSE",
    }.items():
        records.append({"path": name, **download(url, target / name)})
    segmentation_license = destination / "models" / "diarization" / "segmentation-LICENSE"
    if not segmentation_license.is_file():
        raise RuntimeError("The segmentation model's original LICENSE is missing")
    shutil.copy2(segmentation_license, target / "pyannote-segmentation-LICENSE.txt")
    model_info = destination / "models" / "diarization" / "sources.json"
    if model_info.is_file():
        shutil.copy2(model_info, target / "diarization-sources.json")
    whisper_info = destination / "models" / "whisper-onnx-small" / "sources.json"
    if not whisper_info.is_file():
        raise RuntimeError("The Whisper ONNX model provenance is missing")
    shutil.copy2(whisper_info, target / "whisper-onnx-sources.json")
    for path in sorted((destination / "models").rglob("*")):
        if path.is_file() and ".cache" not in path.parts:
            records.append({
                "model_file": path.relative_to(destination).as_posix(),
                "sha256": digest(path), "bytes": path.stat().st_size,
            })
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    destination = args.destination.resolve()
    ensure_no_prohibited_dependencies()
    if not (destination / "models" / "whisper-onnx-small" / ".complete").is_file():
        parser.error("Bundle the small model before collecting its redistribution notices")
    licenses = destination / "licenses"
    sources = destination / "third-party-sources"
    # These are generated build outputs. Never reuse notices/source archives
    # from an older media stack in a newly assembled portable distribution.
    for generated in (licenses, sources):
        if generated.exists():
            shutil.rmtree(generated)
    licenses.mkdir(parents=True, exist_ok=True)
    sources.mkdir(parents=True, exist_ok=True)
    (licenses / ".complete").unlink(missing_ok=True)
    manifest = {
        "python": sys.version,
        "python_distributions": collect_python(licenses, sources),
        "frontend_distributions": collect_frontend(licenses, sources),
        "model_sources": collect_models(destination, licenses),
    }
    shutil.copy2(ROOT / "LICENSE", licenses / "GIJI-SOURCE-MIT-LICENSE.txt")
    # HEAD is the release tag in CI; git archive cannot collect user data or
    # ignored local secrets. It includes the scripts necessary to rebuild giji.
    subprocess.run(
        ["git", "archive", "--format=zip", "--prefix=giji-automemo/",
         "-o", str(sources / "giji-automemo-source.zip"), "HEAD"], cwd=ROOT, check=True,
    )
    manifest["source_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
    ).strip()
    for name in ("pyproject.toml", "frontend/package-lock.json"):
        manifest[name] = digest(ROOT / name)
    (licenses / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (sources / "SOURCES.json").write_text(
        json.dumps({
            "source_commit": manifest["source_commit"],
            "asr_engine": "sherpa-onnx-whisper-int8-v1",
            "dependency_audit": "DEPENDENCY-AUDIT.json",
            "archives": ["giji-automemo-source.zip", "installed-python-source.zip",
                         "frontend-installed-packages.zip"],
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (licenses / "README.txt").write_text(
        "Giji AutoMemo portable distribution — third-party notices\n\n"
        "The application source is MIT licensed. Independently licensed runtime\n"
        "components and model weights retain their original license notices.\n"
        "See manifest.json for installed package versions and model origins.\n\n"
        "Speech recognition uses sherpa-onnx and int8 Whisper ONNX weights.\n"
        "WAV reading uses Python's standard library, with SciPy resampling.\n"
        "Model notices: Whisper, Silero VAD and pyannote segmentation use MIT;\n"
        "the 3D-Speaker embedding model uses Apache-2.0.\n"
        "Python and frontend notices are retained in their own directories.\n"
        "These components are supplied without warranty under their original terms.\n",
        encoding="utf-8",
    )
    (sources / "README.txt").write_text(
        "Build provenance and source material\n\n"
        "Unpack giji-automemo-source.zip and follow docs/distribution.md.\n"
        "The release includes application source, installed Python module sources,\n"
        "frontend dependency packages, and the scripts needed to rebuild it.\n"
        "Use licenses/manifest.json for the build's exact dependency versions.\n"
        "DEPENDENCY-AUDIT.json records native binary hashes and PE imports;\n"
        "the build and publication gates scan source archives recursively too.\n",
        encoding="utf-8",
    )
    (licenses / ".complete").write_text("1\n", encoding="utf-8")
    print("Portable notices and source material collected.", flush=True)


if __name__ == "__main__":
    main()
