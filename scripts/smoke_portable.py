"""Verify real frozen inference and authenticated local UI before publishing a ZIP."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import zipfile


def check(executable: Path) -> None:
    executable = executable.resolve()
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    if os.name == "nt":
        windows = Path(os.environ["SystemRoot"])
        environment["PATH"] = os.pathsep.join([str(windows / "System32"), str(windows)])
    subprocess.run(
        [str(executable), "--portable-self-test"], check=True, timeout=900, env=environment,
    )
    with tempfile.TemporaryDirectory(prefix="giji-launch-test-") as temporary:
        folder = Path(temporary)
        # Do not print launch-url.txt or an authenticated request: they contain
        # the per-launch token. Test data never enters the distribution folder.
        with (folder / "application.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(executable), "--no-browser", "--demo", "--data-dir", str(folder / "data")],
                cwd=folder, env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
            token = None
            failed = False
            try:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"The application exited with status {process.returncode}.")
                    launch_file = folder / "data" / "launch-url.txt"
                    if launch_file.exists():
                        parsed = urlparse(launch_file.read_text(encoding="utf-8"))
                        base_url = f"{parsed.scheme}://{parsed.netloc}"
                        token = parse_qs(parsed.fragment)["token"][0]
                        try:
                            with urlopen(base_url + "/", timeout=3) as response:
                                html = response.read().decode("utf-8")
                            request = Request(
                                base_url + "/api/snapshot",
                                headers={"Authorization": f"Bearer {token}"},
                            )
                            with urlopen(request, timeout=20) as response:
                                snapshot = json.load(response)
                            break
                        except (URLError, TimeoutError, ConnectionError):
                            pass
                    time.sleep(0.5)
                else:
                    raise RuntimeError("The local HTTP application did not become ready within 120 seconds.")
                assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
                assert assets, "The compiled frontend assets are missing."
                for asset in assets:
                    with urlopen(base_url + asset, timeout=5) as response:
                        assert response.status == 200 and response.read(1)
                assert snapshot["health"]["models"]["ready"], "The bundled models are not ready."
                assert snapshot["sessions"], "Demo data was not returned by the packaged app."
                print("Portable smoke passed: frozen inference, local API, bundled models and compiled UI.")
            except Exception:
                failed = True
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if failed:
                    log.flush()
                    log.seek(0)
                    detail = log.read()[-16000:]
                    if token:
                        detail = detail.replace(token, "[redacted]")
                    detail = re.sub(r"(?i)(Bearer\s+|token=)[A-Za-z0-9_-]+", r"\1[redacted]", detail)
                    print("Packaged application diagnostic log:\n" + detail)
                if process.returncode not in (0, 1, -15):
                    # Printing only process status avoids accidental token output.
                    print(f"Smoke app cleanup exit status: {process.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--executable", type=Path)
    group.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.executable:
        check(args.executable)
        return
    with tempfile.TemporaryDirectory(prefix="giji-unzip-test-") as temporary:
        folder = Path(temporary).resolve()
        with zipfile.ZipFile(args.archive) as archive:
            for name in archive.namelist():
                if not (folder / name).resolve().is_relative_to(folder):
                    raise RuntimeError("Unsafe ZIP path.")
            archive.extractall(folder)
        check(folder / "GijiAutoMemo" / "GijiAutoMemo.exe")


if __name__ == "__main__":
    main()
