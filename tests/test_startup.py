"""A legacy audio environment must fail before app startup or data changes."""

import sys

import pytest

from giji import __main__ as entry


def test_prohibited_runtime_stops_before_lock_server_or_browser(tmp_path, monkeypatch, capsys):
    from giji import runtime_dependencies

    directory = tmp_path / "never-created"
    monkeypatch.setattr(sys, "argv", ["giji", "--data-dir", str(directory), "--no-browser"])

    def reject():
        raise runtime_dependencies.ProhibitedDependencyError("Legacy FFmpeg dependency; recreate the environment.")

    monkeypatch.setattr(runtime_dependencies, "ensure_no_prohibited_dependencies", reject)
    monkeypatch.setattr(entry, "acquire_lock", lambda *_: pytest.fail("Startup must stop before acquiring a lock."))
    monkeypatch.setattr(entry.webbrowser, "open", lambda *_: pytest.fail("Browser must not be opened."))
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 1
    assert "recreate the environment" in capsys.readouterr().err
    assert not directory.exists()
