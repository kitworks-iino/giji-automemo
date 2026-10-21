import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.check_python import runtime_error


def supported_runtime(**changes):
    return {
        "platform": "win32", "platform_tag": "win-amd64", "implementation": "cpython",
        "bits": 64, "version": (3, 13), "free_threaded": False, **changes,
    }


@pytest.mark.parametrize("minor", [11, 12, 13])
def test_source_runtime_accepts_supported_standard_windows_python(minor):
    assert runtime_error(supported_runtime(version=(3, minor))) is None


@pytest.mark.parametrize(("changes", "message"), [
    ({"platform": "linux"}, "Windows"),
    ({"implementation": "pypy"}, "CPython"),
    ({"bits": 32}, "x64"),
    ({"platform_tag": "win-arm64"}, "x64"),
    ({"free_threaded": True}, "free-threaded"),
    ({"version": (3, 10)}, "3.13"),
    ({"version": (3, 14)}, "3.13"),
])
def test_native_runtime_rejects_unsupported_abi_before_install(changes, message):
    assert message in runtime_error(supported_runtime(**changes))


def test_portable_requires_exact_python_minor_while_source_retains_compatibility():
    assert runtime_error(supported_runtime(), "3.13") is None
    assert "3.13" in runtime_error(supported_runtime(version=(3, 11)), "3.13")


def setup_copy(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("setup.ps1", "check_python.py"):
        shutil.copyfile(source / name, scripts / name)
    return scripts / "setup.ps1"


def run_powershell(script, *args, env=None):
    shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    assert shell, "Windows CI must exercise the actual PowerShell setup script."
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        capture_output=True, text=True, timeout=30, env=env,
    )


@pytest.mark.skipif(os.name != "nt", reason="Native Windows setup selection")
def test_setup_accepts_company_python_path_without_creating_venv_or_installing(tmp_path):
    result = run_powershell(setup_copy(tmp_path), "-CheckPythonOnly", "-Python", sys.executable)
    assert result.returncode == 0, result.stderr
    assert "Compatible Python:" in result.stdout and sys.executable in result.stdout
    assert not (tmp_path / ".venv").exists()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows PowerShell launcher fallback")
def test_setup_falls_back_to_path_when_installed_launcher_has_no_matching_python(tmp_path):
    setup = setup_copy(tmp_path)
    wrapper = tmp_path / "launcher-fallback.ps1"
    wrapper.write_text(
        "function global:py { $global:LASTEXITCODE = 103; Write-Error 'No matching Python installation.' }\n"
        "function global:python { & $env:GIJI_TEST_PYTHON @args }\n"
        "function global:python3 { & $env:GIJI_TEST_PYTHON @args }\n"
        "& $env:GIJI_TEST_SETUP -CheckPythonOnly\n", encoding="utf-8",
    )
    result = run_powershell(wrapper, env={
        **os.environ, "GIJI_TEST_PYTHON": sys.executable, "GIJI_TEST_SETUP": str(setup),
    })
    assert result.returncode == 0, result.stderr
    assert "Compatible Python:" in result.stdout and sys.executable in result.stdout
    assert not (tmp_path / ".venv").exists()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows existing-venv validation")
def test_setup_validates_and_preserves_existing_supported_venv(tmp_path):
    setup = setup_copy(tmp_path)
    venv = tmp_path / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=30)
    marker = venv / "preserve-me"
    marker.write_text("existing environment", encoding="utf-8")
    python = venv / "Scripts" / "python.exe"
    before = python.stat().st_mtime_ns
    result = run_powershell(setup, "-CheckPythonOnly", "-Python", "nonexistent-requested-python.exe")
    assert result.returncode == 0, result.stderr
    assert str(python) in result.stdout
    assert marker.read_text(encoding="utf-8") == "existing environment"
    assert python.stat().st_mtime_ns == before


@pytest.mark.skipif(os.name != "nt", reason="Native Windows broken-venv handling")
def test_setup_does_not_overwrite_existing_incomplete_environment(tmp_path):
    setup = setup_copy(tmp_path)
    venv = tmp_path / ".venv"
    venv.mkdir()
    marker = venv / "preserve-me"
    marker.write_text("existing environment", encoding="utf-8")
    result = run_powershell(setup, "-CheckPythonOnly", "-Python", sys.executable)
    assert result.returncode != 0
    assert "Move .venv aside" in result.stderr
    assert marker.read_text(encoding="utf-8") == "existing environment"
    assert not (venv / "Scripts").exists()
