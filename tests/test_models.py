import io
import sys
import tarfile

import pytest

from giji.models import (
    DIARIZATION_REQUIRED_FILES,
    ModelError,
    ModelManager,
    SEGMENTATION_MEMBER,
    WHISPER_REQUIRED_FILES,
)


def make_archive(path, *, invalid=False):
    with tarfile.open(path, "w:bz2") as archive:
        model = tarfile.TarInfo(SEGMENTATION_MEMBER)
        if invalid:
            model.type = tarfile.SYMTYPE
            model.linkname = "/etc/passwd"
            archive.addfile(model)
        else:
            model.size = 2000
            archive.addfile(model, io.BytesIO(b"model" * 400))
        # A traversal member must never be extracted.
        malicious = tarfile.TarInfo("../../escaped")
        malicious.size = 4
        archive.addfile(malicious, io.BytesIO(b"oops"))


def test_diarization_download_extracts_only_known_model(tmp_path, monkeypatch):
    manager = ModelManager(tmp_path)
    def fake_fetch(url, destination):
        if destination.name.endswith(".bz2"):
            make_archive(destination)
        else:
            destination.write_bytes(b"embedding" * 300)
    monkeypatch.setattr(manager, "_fetch", fake_fetch)
    manager._download_diarization(lambda _: None)
    assert manager.segmentation_path.read_bytes() == b"model" * 400
    assert manager.embedding_path.is_file()
    assert (manager.segmentation_path.parent / ".complete").is_file()
    assert not (tmp_path / "escaped").exists()


def test_diarization_download_rejects_symlink_model(tmp_path, monkeypatch):
    manager = ModelManager(tmp_path)
    monkeypatch.setattr(manager, "_fetch", lambda url, dest: make_archive(dest, invalid=True))
    with pytest.raises(ModelError):
        manager._download_diarization(lambda _: None)
    assert not manager.segmentation_path.exists()
    assert not (manager.segmentation_path.parent / ".complete").exists()


def test_interrupted_model_download_never_marks_ready(tmp_path, monkeypatch):
    manager = ModelManager(tmp_path)
    def fail(url, destination):
        destination.write_bytes(b"partial")
        raise OSError("disk full")
    monkeypatch.setattr(manager, "_fetch", fail)
    with pytest.raises(OSError):
        manager._download_diarization(lambda _: None)
    assert not (manager.segmentation_path.parent / ".complete").exists()


def complete_model(directory, files):
    directory.mkdir(parents=True, exist_ok=True)
    for filename in files:
        (directory / filename).write_bytes(b"model-fixture")
    return directory


def bundle(directory, *, model="small", diarization=True):
    complete_model(directory / f"whisper-{model}", WHISPER_REQUIRED_FILES)
    if diarization:
        complete_model(directory / "diarization", DIARIZATION_REQUIRED_FILES)
    return directory


def test_frozen_application_discovers_models_next_to_executable(tmp_path, monkeypatch):
    installation = tmp_path / "GijiAutoMemo"
    bundled = bundle(installation / "models")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(installation / "GijiAutoMemo.exe"))
    # CWD and _MEIPASS must not change the trusted, executable-relative location.
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "unrelated"), raising=False)
    monkeypatch.chdir(tmp_path)
    manager = ModelManager(tmp_path / "user")
    assert manager.bundled_directory == bundled
    assert manager.whisper_path({}) == bundled / "whisper-small"
    assert manager.segmentation_path == bundled / "diarization" / "segmentation.onnx"
    assert manager.embedding_path == bundled / "diarization" / "embedding.onnx"
    assert manager.status({})["ready"]
    assert "同梱" in manager.status({})["detail"]
    assert not manager.directory.exists()


def test_source_application_does_not_scan_executable_adjacent_models(tmp_path, monkeypatch):
    bundle(tmp_path / "models")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    manager = ModelManager(tmp_path / "user")
    assert manager.bundled_directory is None
    assert not manager.status({})["ready"]


def test_complete_user_models_take_precedence_over_bundle(tmp_path):
    bundled = bundle(tmp_path / "bundled")
    user = tmp_path / "user"
    bundle(user / "models")
    manager = ModelManager(user, bundled_models=bundled)
    assert manager.whisper_path({}) == user / "models" / "whisper-small"
    assert manager.segmentation_path.parent == user / "models" / "diarization"
    assert manager.embedding_path.parent == user / "models" / "diarization"
    assert "同梱" not in manager.status({})["detail"]


def test_partial_user_models_fall_back_to_complete_bundle(tmp_path):
    bundled = bundle(tmp_path / "bundled")
    user = tmp_path / "user"
    partial_whisper = complete_model(user / "models" / "whisper-small", (".complete", "model.bin"))
    partial_diarization = complete_model(user / "models" / "diarization", (".complete", "segmentation.onnx"))
    manager = ModelManager(user, bundled_models=bundled)
    assert manager.whisper_path({}) == bundled / "whisper-small"
    assert manager.segmentation_path.parent == bundled / "diarization"
    assert manager.embedding_path.parent == bundled / "diarization"
    assert manager.status({})["ready"]
    assert sorted(path.name for path in partial_whisper.iterdir()) == [".complete", "model.bin"]
    assert sorted(path.name for path in partial_diarization.iterdir()) == [".complete", "segmentation.onnx"]


@pytest.mark.parametrize("user_whisper", [True, False])
def test_model_families_resolve_independently(tmp_path, user_whisper):
    bundled = bundle(tmp_path / "bundled")
    user = tmp_path / "user"
    if user_whisper:
        complete_model(user / "models" / "whisper-small", WHISPER_REQUIRED_FILES)
    else:
        complete_model(user / "models" / "diarization", DIARIZATION_REQUIRED_FILES)
    manager = ModelManager(user, bundled_models=bundled)
    whisper_parent = user / "models" if user_whisper else bundled
    diarization_parent = bundled if user_whisper else user / "models"
    assert manager.whisper_path({}) == whisper_parent / "whisper-small"
    assert manager.segmentation_path.parent == diarization_parent / "diarization"
    assert manager.embedding_path.parent == diarization_parent / "diarization"
    assert manager.status({})["ready"]


def test_zero_byte_or_incomplete_bundle_is_not_ready(tmp_path):
    bundled = bundle(tmp_path / "bundled")
    (bundled / "whisper-small" / "model.bin").write_bytes(b"")
    manager = ModelManager(tmp_path / "user", bundled_models=bundled)
    assert not manager.status({})["ready"]
    assert "文字起こし" in manager.status({})["detail"]
    assert manager.whisper_path({}) == manager.directory / "whisper-small"
    (bundled / "whisper-small" / "model.bin").write_bytes(b"valid")
    (bundled / "diarization" / "embedding.onnx").unlink()
    assert not manager.status({})["ready"]
    assert "話者分離" in manager.status({})["detail"]
    assert manager.status({"diarization": False})["ready"]


def test_missing_selected_model_does_not_silently_substitute_small(tmp_path):
    bundled = bundle(tmp_path / "bundled")
    manager = ModelManager(tmp_path / "user", bundled_models=bundled)
    assert manager.whisper_path({"whisper_model": "medium"}) == manager.directory / "whisper-medium"
    assert not manager.status({"whisper_model": "medium"})["ready"]
    assert "medium" in manager.status({"whisper_model": "medium"})["detail"]


def test_download_with_complete_bundle_never_copies_writes_or_requests(tmp_path, monkeypatch):
    bundled = bundle(tmp_path / "bundled")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in bundled.rglob("*") if path.is_file()}
    manager = ModelManager(tmp_path / "user", bundled_models=bundled)
    monkeypatch.setattr(manager, "_fetch", lambda *args: pytest.fail("Bundled model must not download"))
    messages = []
    manager.download({}, messages.append)
    assert manager.status({})["ready"]
    assert not manager.directory.exists()
    assert messages == ["ローカルモデルの準備が完了しました。"]
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in bundled.rglob("*") if path.is_file()}
    assert before == after


def test_diarization_repair_only_writes_user_directory(tmp_path, monkeypatch):
    bundled = bundle(tmp_path / "bundled")
    (bundled / "diarization" / "embedding.onnx").unlink()
    manager = ModelManager(tmp_path / "user", bundled_models=bundled)
    def fetch(url, destination):
        if destination.name.endswith(".bz2"):
            make_archive(destination)
        else:
            destination.write_bytes(b"embedding" * 300)
    monkeypatch.setattr(manager, "_fetch", fetch)
    manager.download({}, lambda _: None)
    assert manager.whisper_path({}) == bundled / "whisper-small"
    assert manager.segmentation_path.parent == manager.directory / "diarization"
    assert manager.embedding_path.parent == manager.directory / "diarization"
    assert manager.status({})["ready"]
    assert not (bundled / "diarization" / "embedding.onnx").exists()
    assert not (manager.directory / "whisper-small").exists()
