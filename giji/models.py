"""Explicit, local model downloads. Importing this module never downloads a model."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from typing import Callable

import httpx


WHISPER_MODELS = {"tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"}
SEGMENTATION_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/"
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)
SEGMENTATION_MEMBER = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
WHISPER_REQUIRED_FILES = (".complete", "model.bin", "config.json", "tokenizer.json")
DIARIZATION_REQUIRED_FILES = (".complete", "segmentation.onnx", "embedding.onnx")


class ModelError(RuntimeError):
    """A message safe to show in the UI."""


class ModelManager:
    def __init__(self, data_dir: Path, *, bundled_models: Path | None = None):
        self.directory = Path(data_dir) / "models"
        # PyInstaller onedir layout: App.exe, _internal/, models/. Resolve from
        # the executable, never the working directory or an environment override.
        # Source runs use only user models unless a bundle is injected by tests.
        self.bundled_directory = (
            Path(bundled_models).resolve() if bundled_models is not None
            else Path(sys.executable).resolve().parent / "models" if getattr(sys, "frozen", False)
            else None
        )

    @staticmethod
    def _complete(directory: Path, filenames: tuple[str, ...]) -> bool:
        """Completion markers alone do not establish a usable model."""
        try:
            return all((directory / name).is_file() and (directory / name).stat().st_size > 0
                       for name in filenames)
        except OSError:
            return False

    def _resolve(self, subdirectory: str, required: tuple[str, ...]) -> Path:
        user_directory = self.directory / subdirectory
        if self._complete(user_directory, required):
            return user_directory
        if self.bundled_directory is not None:
            bundled_directory = self.bundled_directory / subdirectory
            if self._complete(bundled_directory, required):
                return bundled_directory
        # Missing/incomplete models always resolve to the writable destination.
        return user_directory

    @property
    def segmentation_path(self) -> Path:
        return self._resolve("diarization", DIARIZATION_REQUIRED_FILES) / "segmentation.onnx"

    @property
    def embedding_path(self) -> Path:
        return self._resolve("diarization", DIARIZATION_REQUIRED_FILES) / "embedding.onnx"

    @staticmethod
    def _model_name(settings: dict) -> str:
        model = settings.get("whisper_model", "small")
        if model not in WHISPER_MODELS:
            raise ModelError("対応していない文字起こしモデルです。")
        return model

    def whisper_path(self, settings: dict) -> Path:
        model = self._model_name(settings)
        return self._resolve(f"whisper-{model}", WHISPER_REQUIRED_FILES)

    def status(self, settings: dict) -> dict:
        try:
            directory = self.whisper_path(settings)
        except ModelError as exc:
            return {"ready": False, "detail": str(exc)}
        if not self._complete(directory, WHISPER_REQUIRED_FILES):
            return {"ready": False,
                    "detail": f"文字起こしモデル「{self._model_name(settings)}」がありません。設定からダウンロードしてください。"}
        directories = [directory]
        if settings.get("diarization", True):
            diarization_directory = self.segmentation_path.parent
            if not self._complete(diarization_directory, DIARIZATION_REQUIRED_FILES):
                return {"ready": False, "detail": "話者分離モデルがありません。設定からダウンロードしてください。"}
            directories.append(diarization_directory)
        bundled = self.bundled_directory is not None and any(
            path.parent == self.bundled_directory for path in directories
        )
        return {"ready": True,
                "detail": "同梱のローカルモデルを準備済み（ダウンロード不要）" if bundled else "ローカルモデルを準備済み"}

    def download(self, settings: dict, progress_callback: Callable[[str], None]) -> None:
        model = self._model_name(settings)
        directory = self.directory / f"whisper-{model}"
        try:
            if not self._complete(self.whisper_path(settings), WHISPER_REQUIRED_FILES):
                from faster_whisper.utils import download_model

                progress_callback("文字起こしモデルをダウンロードしています。初回は数分かかります。")
                directory.mkdir(parents=True, exist_ok=True)
                download_model(model, output_dir=str(directory))
                required = ["model.bin", "config.json", "tokenizer.json"]
                if not self._complete(directory, tuple(required)):
                    raise ModelError("文字起こしモデルの取得が不完全です。再実行してください。")
                (directory / ".complete").write_text("1\n", encoding="utf-8")
            if settings.get("diarization", True):
                self._download_diarization(progress_callback)
        except ModelError:
            raise
        except ImportError as exc:
            raise ModelError("音声処理ライブラリがありません。セットアップを再実行してください。") from exc
        except Exception as exc:
            raise ModelError(
                "モデルの取得に失敗しました。ネット接続と空き容量を確認し、再実行してください。"
            ) from exc
        progress_callback("ローカルモデルの準備が完了しました。")

    def _download_diarization(self, callback: Callable[[str], None]) -> None:
        if self._complete(self.segmentation_path.parent, DIARIZATION_REQUIRED_FILES):
            return
        # Always write to the user's data directory, even when the installation
        # contains an incomplete or read-only bundle.
        directory = self.directory / "diarization"
        segmentation_path = directory / "segmentation.onnx"
        embedding_path = directory / "embedding.onnx"
        directory.mkdir(parents=True, exist_ok=True)
        callback("話者分離モデルをダウンロードしています。")
        with tempfile.TemporaryDirectory(prefix="giji-model-", dir=self.directory) as temporary:
            temporary = Path(temporary)
            if not self._complete(directory, ("segmentation.onnx",)):
                archive_path = temporary / "segmentation.tar.bz2"
                self._fetch(SEGMENTATION_URL, archive_path)
                with tarfile.open(archive_path, "r:bz2") as archive:
                    member = archive.getmember(SEGMENTATION_MEMBER)
                    if not member.isfile() or member.size > 100_000_000:
                        raise ModelError("話者分離モデルの形式が正しくありません。")
                    # Read one known regular file, never extract archive paths.
                    source = archive.extractfile(member)
                    if source is None:
                        raise ModelError("話者分離モデルを読み取れませんでした。")
                    with source, (temporary / "segmentation.onnx").open("wb") as target:
                        shutil.copyfileobj(source, target)
                    os.replace(temporary / "segmentation.onnx", segmentation_path)
                    for name in ("LICENSE", "README.md"):
                        try:
                            item = archive.getmember(f"sherpa-onnx-pyannote-segmentation-3-0/{name}")
                            if item.isfile() and item.size < 1_000_000:
                                stream = archive.extractfile(item)
                                if stream:
                                    with stream:
                                        (directory / f"segmentation-{name}").write_bytes(stream.read())
                        except KeyError:
                            pass
            if not self._complete(directory, ("embedding.onnx",)):
                self._fetch(EMBEDDING_URL, temporary / "embedding.onnx")
                os.replace(temporary / "embedding.onnx", embedding_path)
        (directory / "sources.json").write_text(
            json.dumps({"segmentation": SEGMENTATION_URL, "embedding": EMBEDDING_URL}, indent=2),
            encoding="utf-8",
        )
        (directory / ".complete").write_text("1\n", encoding="utf-8")

    @staticmethod
    def _fetch(url: str, destination: Path) -> None:
        # Only fixed upstream URLs are passed here. TLS verification stays enabled.
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=120)) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with destination.open("wb") as file:
                    for chunk in response.iter_bytes(1024 * 1024):
                        file.write(chunk)
        if destination.stat().st_size < 1024:
            raise ModelError("取得したモデルファイルが不完全です。")
