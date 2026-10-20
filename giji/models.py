"""Explicit, local model downloads. Importing this module never downloads a model."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
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


class ModelError(RuntimeError):
    """A message safe to show in the UI."""


class ModelManager:
    def __init__(self, data_dir: Path):
        self.directory = Path(data_dir) / "models"
        self.segmentation_path = self.directory / "diarization" / "segmentation.onnx"
        self.embedding_path = self.directory / "diarization" / "embedding.onnx"

    def whisper_path(self, settings: dict) -> Path:
        model = settings.get("whisper_model", "small")
        if model not in WHISPER_MODELS:
            raise ModelError("対応していない文字起こしモデルです。")
        return self.directory / f"whisper-{model}"

    def status(self, settings: dict) -> dict:
        try:
            directory = self.whisper_path(settings)
        except ModelError as exc:
            return {"ready": False, "detail": str(exc)}
        # A completion marker prevents partial downloads being accepted as ready.
        required = [
            directory / ".complete",
            directory / "model.bin",
            directory / "config.json",
            directory / "tokenizer.json",
        ]
        if settings.get("diarization", True):
            required += [
                self.segmentation_path,
                self.embedding_path,
                self.segmentation_path.parent / ".complete",
            ]
        ready = all(p.is_file() and p.stat().st_size > 0 for p in required)
        return {
            "ready": ready,
            "detail": "ローカルモデルを準備済み" if ready else "設定から音声モデルをダウンロードしてください。",
        }

    def download(self, settings: dict, progress_callback: Callable[[str], None]) -> None:
        directory = self.whisper_path(settings)
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            whisper_files = (".complete", "model.bin", "config.json", "tokenizer.json")
            if not all((directory / name).is_file() and (directory / name).stat().st_size > 0 for name in whisper_files):
                from faster_whisper.utils import download_model

                progress_callback("文字起こしモデルをダウンロードしています。初回は数分かかります。")
                directory.mkdir(parents=True, exist_ok=True)
                download_model(settings.get("whisper_model", "small"), output_dir=str(directory))
                required = ["model.bin", "config.json", "tokenizer.json"]
                if not all((directory / f).is_file() for f in required):
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
        directory = self.segmentation_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if (
            (directory / ".complete").is_file()
            and self.segmentation_path.is_file()
            and self.embedding_path.is_file()
        ):
            return
        callback("話者分離モデルをダウンロードしています。")
        with tempfile.TemporaryDirectory(prefix="giji-model-", dir=self.directory) as temporary:
            temporary = Path(temporary)
            if not self.segmentation_path.is_file():
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
                    os.replace(temporary / "segmentation.onnx", self.segmentation_path)
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
            if not self.embedding_path.is_file():
                self._fetch(EMBEDDING_URL, temporary / "embedding.onnx")
                os.replace(temporary / "embedding.onnx", self.embedding_path)
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
