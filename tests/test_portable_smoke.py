import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import scripts.smoke_portable as smoke


def windows_launch(monkeypatch, *, assignment_error=None):
    events = []
    job = SimpleNamespace(Close=Mock(side_effect=lambda: events.append("close-job")))
    handle = SimpleNamespace(Close=Mock(side_effect=lambda: events.append("close-process-handle")))
    process = Mock(pid=321)
    process.poll.return_value = None
    process.wait.return_value = 1
    active_counts = iter([2, 1, 0])

    def query(_job, kind):
        assert _job is job
        if kind == "limits":
            return {"BasicLimitInformation": {"LimitFlags": 0}}
        events.append("query-owned-processes")
        return {"ActiveProcesses": next(active_counts)}

    def assign(_job, _handle):
        assert _job is job and _handle is handle
        events.append("assign")
        if assignment_error:
            raise assignment_error

    job_api = SimpleNamespace(
        CreateJobObject=Mock(return_value=job),
        QueryInformationJobObject=Mock(side_effect=query),
        SetInformationJobObject=Mock(),
        AssignProcessToJobObject=Mock(side_effect=assign),
        TerminateJobObject=Mock(side_effect=lambda *_: events.append("terminate-owned-tree")),
        JobObjectExtendedLimitInformation="limits",
        JobObjectBasicAccountingInformation="accounting",
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000,
    )
    native_api = SimpleNamespace(OpenProcess=Mock(return_value=handle))
    native_constants = SimpleNamespace(PROCESS_SET_QUOTA=0x100, PROCESS_TERMINATE=1)
    psutil = SimpleNamespace(Process=Mock(return_value=SimpleNamespace(
        resume=Mock(side_effect=lambda: events.append("resume")),
    )))
    monkeypatch.setattr(smoke, "os", SimpleNamespace(name="nt"))
    for name, module in {"win32job": job_api, "win32api": native_api,
                         "win32con": native_constants, "psutil": psutil}.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(smoke.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(smoke.time, "sleep", lambda _: None)
    return SimpleNamespace(events=events, job=job, process=process, job_api=job_api,
                           native_api=native_api, psutil=psutil)


def test_windows_smoke_assigns_suspended_app_before_resume_and_waits_for_owned_descendants(monkeypatch):
    native = windows_launch(monkeypatch)
    application = smoke._launch_application(["isolated-test.exe"], cwd="test-folder", env={}, stdout=None)
    assert smoke.subprocess.Popen.call_args.kwargs["creationflags"] == 0x00000004
    native.job_api.CreateJobObject.assert_called_once_with(None, None)
    limits = native.job_api.SetInformationJobObject.call_args.args[2]
    assert limits["BasicLimitInformation"]["LimitFlags"] & 0x2000
    assert native.events[:3] == ["assign", "close-process-handle", "resume"]
    native.native_api.OpenProcess.assert_called_once_with(0x101, False, 321)
    native.psutil.Process.assert_called_once_with(321)
    application.close()
    assert native.events[-5:] == ["terminate-owned-tree", "query-owned-processes",
                                  "query-owned-processes", "query-owned-processes", "close-job"]
    native.process.wait.assert_called_once_with(timeout=15)
    native.process.terminate.assert_not_called()
    native.process.kill.assert_not_called()


def test_job_assignment_failure_never_resumes_untracked_app(monkeypatch):
    native = windows_launch(monkeypatch, assignment_error=RuntimeError("assignment refused"))
    with pytest.raises(RuntimeError, match="assignment refused"):
        smoke._launch_application(["isolated-test.exe"], cwd="test-folder", env={}, stdout=None)
    native.psutil.Process.assert_not_called()
    native.process.kill.assert_called_once()
    native.process.wait.assert_called_once_with(timeout=5)
    native.job.Close.assert_called_once()


def test_launch_error_remains_primary_if_process_cleanup_also_fails(monkeypatch, capsys):
    native = windows_launch(monkeypatch, assignment_error=RuntimeError("original assignment failure"))
    native.process.kill.side_effect = OSError("secondary cleanup failure")
    with pytest.raises(RuntimeError, match="original assignment failure"):
        smoke._launch_application(["isolated-test.exe"], cwd="test-folder", env={}, stdout=None)
    native.job.Close.assert_called_once()
    assert "cleanup also failed" in capsys.readouterr().out


def test_owned_process_wait_is_bounded_and_job_handle_closes_on_timeout(monkeypatch):
    native = windows_launch(monkeypatch)
    application = smoke._launch_application(["isolated-test.exe"], cwd="test-folder", env={}, stdout=None)
    native.job_api.QueryInformationJobObject.side_effect = lambda *_: {"ActiveProcesses": 1}
    ticks = iter([0, 16])
    monkeypatch.setattr(smoke, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=Mock()))
    with pytest.raises(TimeoutError, match="process tree"):
        application.close()
    native.job.Close.assert_called_once()
    native.process.wait.assert_called_once_with(timeout=15)


def test_temporary_directory_retries_transient_windows_handle_locks(monkeypatch, tmp_path):
    temporary = SimpleNamespace(name=str(tmp_path), cleanup=Mock(side_effect=[PermissionError(), None]))
    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", lambda **_: temporary)
    monkeypatch.setattr(smoke, "time", SimpleNamespace(monotonic=lambda: 0, sleep=Mock()))
    with smoke._temporary_directory("test-") as folder:
        assert folder == tmp_path
    assert temporary.cleanup.call_count == 2
    smoke.time.sleep.assert_called_once_with(0.05)


@pytest.mark.parametrize("original_failure", [False, True])
def test_temporary_cleanup_is_bounded_and_preserves_original_failure(
    monkeypatch, tmp_path, capsys, original_failure,
):
    temporary = SimpleNamespace(name=str(tmp_path), cleanup=Mock(side_effect=PermissionError("locked")))
    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", lambda **_: temporary)
    ticks = iter([0, 6])
    monkeypatch.setattr(smoke, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=Mock()))
    expected = ValueError if original_failure else PermissionError
    with pytest.raises(expected, match="original smoke failure" if original_failure else "locked"):
        with smoke._temporary_directory("test-"):
            if original_failure:
                raise ValueError("original smoke failure")
    temporary.cleanup.assert_called_once()
    if original_failure:
        assert "cleanup also failed" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Job Object lifecycle")
def test_native_windows_job_stops_only_smoke_parent_and_child_before_file_cleanup(tmp_path):
    import psutil

    child_code = (
        "from pathlib import Path; import time; "
        "held = open('held-file.txt', 'w'); "
        "Path('child-ready').write_text('ready'); time.sleep(60)"
    )
    parent_code = (
        "import subprocess, sys, time; from pathlib import Path; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "Path('child.pid').write_text(str(child.pid)); time.sleep(60)"
    )
    # This independently launched control process must never enter the job.
    control = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    application = None
    try:
        application = smoke._launch_application(
            [sys.executable, "-c", parent_code], cwd=tmp_path,
            env=os.environ.copy(), stdout=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while not (tmp_path / "child-ready").exists() or not (tmp_path / "child.pid").exists():
            assert application.process.poll() is None, "The assigned parent exited before creating its child."
            assert time.monotonic() < deadline, "The assigned child did not become ready."
            time.sleep(0.05)
        child = psutil.Process(int((tmp_path / "child.pid").read_text()))
        assert child.is_running()
        application.close()
        assert application.process.poll() is not None
        assert not child.is_running(), "The smoke child survived termination of its job."
        assert control.poll() is None, "An independent process was incorrectly included in the job."
        (tmp_path / "held-file.txt").unlink()  # The child's Windows file handle is released.
    finally:
        try:
            if application is not None:
                application.close()
        finally:
            control.terminate()
            control.wait(timeout=5)
