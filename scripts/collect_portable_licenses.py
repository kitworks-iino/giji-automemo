"""Collect redistribution notices and upstream source material for a portable build.

Run with the same clean virtual environment used by PyInstaller. Downloads are
build-time only. Downloaded Python is parsed as data and is never executed.
"""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile, ZIP_DEFLATED


ROOT = Path(__file__).resolve().parent.parent
AV_VERSION = "16.1.0"
FFMPEG_BUILD = "8.0.1-3"
BUILD_SCRIPT = (
    f"https://raw.githubusercontent.com/PyAV-Org/pyav-ffmpeg/{FFMPEG_BUILD}/scripts/build-ffmpeg.py"
)
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
    if parsed.scheme == "http" and parsed.hostname in {"deb.debian.org", "downloads.xiph.org"}:
        url = "https:" + url[5:]
        parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError(f"Unsafe source URL: {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and (expected_sha256 is None or digest(destination) == expected_sha256):
        return {"url": url, "sha256": digest(destination), "bytes": destination.stat().st_size}
    temporary = destination.with_name(destination.name + ".partial")
    for attempt in range(4):
        try:
            request = Request(url, headers={"User-Agent": "giji-automemo-source-collector/0.3"})
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


def ffmpeg_packages(source: str) -> list[dict]:
    """Read literal download declarations without importing upstream build code."""
    packages = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "Package":
            continue
        literal = {}
        for keyword in node.keywords:
            if keyword.arg in {"name", "source_url", "sha256", "source_filename"}:
                literal[keyword.arg] = ast.literal_eval(keyword.value)
        if not {"name", "source_url", "sha256"} <= literal.keys():
            raise RuntimeError("Upstream FFmpeg source declaration was not a literal")
        # These groups are disabled by upstream for Windows. Include opencore
        # because the upstream release workflow explicitly builds --community.
        if literal["name"] in {"alsa-lib", "gmp", "unistring", "nettle", "gnutls"}:
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", literal["sha256"]):
            raise RuntimeError("Upstream FFmpeg source hash was invalid")
        packages.append(literal)
    if not {"ffmpeg", "x264", "x265", "opencore-amr"} <= {item["name"] for item in packages}:
        raise RuntimeError("Unexpected upstream FFmpeg build definition")
    return packages


def collect_ffmpeg(licenses: Path, sources: Path) -> list[dict]:
    if metadata.version("av") != AV_VERSION:
        raise RuntimeError(f"Portable source collector requires av=={AV_VERSION}")
    records = []
    build = sources / f"pyav-ffmpeg-{FFMPEG_BUILD}-build.py"
    records.append({"path": build.name, **download(BUILD_SCRIPT, build)})
    packages = ffmpeg_packages(build.read_text(encoding="utf-8"))

    def fetch_package(package: dict) -> dict:
        filename = package.get("source_filename") or Path(urlparse(package["source_url"]).path).name
        target = sources / "ffmpeg-dependencies" / safe_component(package["name"]) / safe_component(filename)
        record = download(package["source_url"], target, package["sha256"])
        # Extract notice text only, using streamed reads of regular archive
        # members. Never extract archive filesystem paths or links.
        with tarfile.open(target, "r:*") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if (not member.isfile() or member.size > 5 * 1024 * 1024
                        or path.is_absolute() or ".." in path.parts or not is_notice(path)):
                    continue
                stream = archive.extractfile(member)
                if stream is not None:
                    destination = licenses / "ffmpeg" / safe_component(package["name"]) / Path(*path.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with stream:
                        destination.write_bytes(stream.read())
        print(f"Source verified: {package['name']}", flush=True)
        return {"name": package["name"], "path": target.relative_to(sources).as_posix(), **record}

    with ThreadPoolExecutor(max_workers=4) as executor:
        records.extend(executor.map(fetch_package, packages))
    for name, url in {
        f"PyAV-{AV_VERSION}.tar.gz": f"https://github.com/PyAV-Org/PyAV/archive/refs/tags/v{AV_VERSION}.tar.gz",
        f"pyav-ffmpeg-{FFMPEG_BUILD}.tar.gz":
            f"https://github.com/PyAV-Org/pyav-ffmpeg/archive/refs/tags/{FFMPEG_BUILD}.tar.gz",
    }.items():
        records.append({"path": name, **download(url, sources / name)})
    gpl = licenses / "GNU-GPL-v3.txt"
    records.append({
        "path": "../licenses/" + gpl.name,
        **download("https://raw.githubusercontent.com/FFmpeg/FFmpeg/n8.0.1/COPYING.GPLv3", gpl),
    })
    return records


def collect_models(destination: Path, licenses: Path) -> list[dict]:
    target = licenses / "models"
    target.mkdir(parents=True, exist_ok=True)
    records = []
    for name, url in {
        "whisper-LICENSE.txt": "https://raw.githubusercontent.com/openai/whisper/main/LICENSE",
        "faster-whisper-small-README.md": "https://huggingface.co/Systran/faster-whisper-small/resolve/main/README.md",
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
    if not (destination / "models" / "whisper-small" / ".complete").is_file():
        parser.error("Bundle the small model before collecting its redistribution notices")
    licenses = destination / "licenses"
    sources = destination / "third-party-sources"
    licenses.mkdir(parents=True, exist_ok=True)
    sources.mkdir(parents=True, exist_ok=True)
    (licenses / ".complete").unlink(missing_ok=True)
    manifest = {
        "python": sys.version,
        "python_distributions": collect_python(licenses, sources),
        "frontend_distributions": collect_frontend(licenses, sources),
        "model_sources": collect_models(destination, licenses),
        "ffmpeg_sources": collect_ffmpeg(licenses, sources),
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
            "ffmpeg_build": FFMPEG_BUILD,
            "av_version": AV_VERSION,
            "archives": manifest["ffmpeg_sources"],
            "availability_note": (
                "PyAV, FFmpeg and codec sources are from the upstream versions recorded here. "
                "The pinned upstream Windows workflow installs MinGW runtime libraries from "
                "rolling MSYS2 packages without recording their resolved package versions. "
                "This collection therefore preserves published build provenance but does not "
                "claim an exact source snapshot for those MinGW runtime binaries."
            ),
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (licenses / "README.txt").write_text(
        "Giji AutoMemo portable distribution — third-party notices\n\n"
        "The original giji-automemo source is MIT licensed. This portable archive\n"
        "also contains independently licensed components and model weights.\n"
        "The MIT license does not replace their terms.\n\n"
        "PyAV 16.1.0's upstream Windows wheel contains the community FFmpeg 8.0.1\n"
        "build, including GPL components. GPLv3 terms apply to the GPL-covered\n"
        "combined distribution; see GNU-GPL-v3.txt. The original source components\n"
        "retain their own license notices. No additional restriction on copying,\n"
        "modification or redistribution of GPL-covered work is imposed by giji.\n\n"
        "third-party-sources/ contains this release's original application source,\n"
        "installed Python module sources, frontend dependency packages, the exact\n"
        "upstream PyAV/FFmpeg build scripts and codec source archives. The codec\n"
        "archives are checked against hashes in the pinned upstream build script.\n"
        "See manifest.json for exact package versions, checksums and origin URLs.\n"
        "The upstream FFmpeg workflow obtains MinGW runtime libraries from its\n"
        "MSYS2 build environment; its workflow records that toolchain provenance.\n"
        "Bundled source material includes the upstream build process as published;\n"
        "it does not assert a bit-for-bit reproducible MinGW toolchain snapshot.\n\n"
        "Model notices: Whisper and pyannote segmentation use MIT; the 3D-Speaker\n"
        "embedding model uses Apache-2.0. See models/ for original notices.\n"
        "Python and frontend notices are retained in their respective directories.\n"
        "These components are supplied without warranty under their original terms.\n",
        encoding="utf-8",
    )
    (sources / "README.txt").write_text(
        "Build provenance and source material\n\n"
        "Unpack giji-automemo-source.zip, follow docs/distribution.md and the\n"
        "checked-in scripts/build-portable.ps1 and PyInstaller specification.\n"
        "Use licenses/manifest.json for the build's installed dependency versions.\n"
        "PyAV's scripts/ffmpeg-latest.json identifies the pinned pyav-ffmpeg release.\n"
        "The pyav-ffmpeg source archive includes its cibuildpkg helpers, patches,\n"
        "build script, and Windows/MSYS2 workflow. FFmpeg and codec archives are\n"
        "unmodified upstream archives under ffmpeg-dependencies/.\n"
        "Build tools and operating-system components are obtained by the upstream\n"
        "workflow as recorded there; this is not a prebuilt build environment.\n",
        encoding="utf-8",
    )
    (licenses / ".complete").write_text("1\n", encoding="utf-8")
    print("Portable notices and source material collected.", flush=True)


if __name__ == "__main__":
    main()
