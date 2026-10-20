import io
import tarfile

import pytest

from giji.models import ModelError, ModelManager, SEGMENTATION_MEMBER


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
