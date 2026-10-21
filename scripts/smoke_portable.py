"""Verify real frozen inference and authenticated local UI before publishing a ZIP."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
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


class _SmokeApplication:
    """Own only the process tree created for this temporary smoke instance."""

    def __init__(self, process, job=None, job_api=None):
        self.process = process
        self.job = job
        self.job_api = job_api

    def close(self) -> None:
        try:
            if self.job is not None:
                try:
                    # A Windows terminate() only stops the parent. A UIA reader
                    # can still hold the extracted executable and temporary cwd.
                    self.job_api.TerminateJobObject(self.job, 1)
                    deadline = time.monotonic() + 15
                    while self.job_api.QueryInformationJobObject(
                        self.job, self.job_api.JobObjectBasicAccountingInformation,
                    )["ActiveProcesses"]:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("The smoke application's process tree did not stop.")
                        time.sleep(0.05)
                finally:
                    self.job.Close()  # KILL_ON_JOB_CLOSE also covers error paths.
                    self.job = None
            elif self.process.poll() is None:
                self.process.terminate()
        finally:
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def _launch_application(command, *, cwd, env, stdout) -> _SmokeApplication:
    if os.name != "nt":
        return _SmokeApplication(subprocess.Popen(
            command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
        ))

    import psutil
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, None)  # NULL security attributes: non-inheritable handle.
    process = None
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        # Assign while suspended, before the app can create a reader subprocess.
        # The job contains this app and its descendants; no existing process is
        # enumerated, attached, or stopped (including the user's Teams).
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
            creationflags=0x00000004,  # CREATE_SUSPENDED
        )
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                      False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        psutil.Process(process.pid).resume()
        return _SmokeApplication(process, job, win32job)
    except BaseException:
        # Assignment/import/resume failure leaves a suspended app, never an
        # untracked running child. Do not mask the original launch failure.
        try:
            if process is not None:
                process.kill()
                process.wait(timeout=5)
        except Exception:
            print("Suspended smoke-process cleanup also failed.")
        finally:
            try:
                job.Close()
            except Exception:
                print("Smoke job cleanup also failed.")
        raise


@contextmanager
def _temporary_directory(prefix: str):
    temporary = tempfile.TemporaryDirectory(prefix=prefix)
    failed = False
    try:
        yield Path(temporary.name)
    except BaseException:
        failed = True
        raise
    finally:
        # Windows can briefly retain filesystem handles after process exit.
        # Retry a bounded interval, without replacing an earlier test failure.
        deadline = time.monotonic() + 5
        while True:
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    if failed:
                        print("Smoke temporary-directory cleanup also failed.")
                        break
                    raise
                time.sleep(0.05)


def check(executable: Path) -> None:
    executable = executable.resolve()
    environment = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    if os.name == "nt":
        windows = Path(os.environ["SystemRoot"])
        environment["PATH"] = os.pathsep.join([str(windows / "System32"), str(windows)])
    # Invalid arguments stop before any journal/registry access. This verifies
    # the cleanup CLI dispatch is packaged without changing the runner's policy.
    cleanup = subprocess.run(
        [str(executable), "--teams-connection-cleanup"], capture_output=True,
        text=True, timeout=30, env=environment, check=False,
    )
    assert cleanup.returncode == 2 and not cleanup.stdout and not cleanup.stderr, (
        "The packaged Teams cleanup entry point did not reject invalid arguments cleanly."
    )
    if environment.get("GITHUB_ACTIONS") == "true":
        # Hosted Windows runners have no signed-in Teams. Exercise the real
        # bounded UIA worker there; never enumerate a local builder's Teams.
        uia = subprocess.run(
            [str(executable), "--teams-uia-calendar-worker"], capture_output=True,
            text=True, encoding="utf-8", timeout=20, env=environment, check=False,
        )
        assert len(uia.stdout) <= 4096 and not uia.stderr, "Unexpected UIA worker diagnostics."
        result = json.loads(uia.stdout)
        assert uia.returncode == 1 and result.get("ok") is False
        assert result.get("error") in {"not_running", "unreadable", "calendar"}, (
            "The native Teams UIA worker failed to load its packaged dependencies."
        )
    subprocess.run(
        [str(executable), "--portable-self-test"], check=True, timeout=900, env=environment,
    )
    with _temporary_directory(prefix="giji-launch-test-") as folder:
        # Do not print launch-url.txt or an authenticated request: they contain
        # the per-launch token. Test data never enters the distribution folder.
        with (folder / "application.log").open("w+", encoding="utf-8") as log:
            application = _launch_application(
                [str(executable), "--no-browser", "--demo", "--data-dir", str(folder / "data")],
                cwd=folder, env=environment, stdout=log,
            )
            process = application.process
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
                assert snapshot["settings"]["calendar_source"] == "teams_desktop"
                assert snapshot["settings"]["teams_calendar_mode"] == "uia"
                assert snapshot["teams_connection"]["state"] == "needs_setup"
                assert not snapshot["teams_connection"]["connected"]
                assert not snapshot["teams_connection"]["debug_active"]
                assert snapshot["teams_connection"]["policy_restored"]
                assert not (folder / "data" / "teams-connection-journal.json").exists()
                print("Portable smoke passed: frozen inference, setup-neutral Teams, local API, bundled models and UI.")
            except Exception:
                failed = True
                raise
            finally:
                try:
                    application.close()
                except Exception:
                    if not failed:
                        raise
                    print("Smoke application cleanup also failed.")
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
    with _temporary_directory(prefix="giji-unzip-test-") as temporary:
        folder = temporary.resolve()
        with zipfile.ZipFile(args.archive) as archive:
            for name in archive.namelist():
                if not (folder / name).resolve().is_relative_to(folder):
                    raise RuntimeError("Unsafe ZIP path.")
            archive.extractall(folder)
        check(folder / "GijiAutoMemo" / "GijiAutoMemo.exe")


if __name__ == "__main__":
    main()
