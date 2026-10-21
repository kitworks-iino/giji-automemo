# 主な依存物・モデル

このアプリのソースはMITですが、依存パッケージやモデルのライセンスを置き換えません。
v0.5のWindowsポータブルZIPはPython実行環境、ONNX版Whisper small、発話検出・話者分離モデルを同梱します。
ZIP内の `licenses/` に第三者のライセンス・通知、`third-party-sources/` に収集した対応ソースと
取得元・ハッシュの記録を含めます。配布物全体がMITのみで構成されているわけではありません。
モデルの重み・実行バイナリはGitのソースツリーには含めません。

v0.5ではFFmpeg・PyAV・faster-whisperを使用しません。旧版からの変更、依存関係と配布物の検査範囲は
[音声ライブラリと配布物の確認](audio-dependencies.md)を参照してください。

| 用途 | プロジェクト・資料 |
| --- | --- |
| 同梱Python実行環境 | [CPython 3.13 / PSFライセンス等](https://docs.python.org/3.13/license.html)。ビルドに使用した版のライセンス・対応ソースを収集 |
| ローカル文字起こし・話者分離 | [sherpa-onnx 1.13.8 / Apache-2.0](https://github.com/k2-fsa/sherpa-onnx/tree/v1.13.8) |
| Whisper重み | [OpenAI Whisper / MIT](https://github.com/openai/whisper)を基にした[sherpa-onnx用ONNXモデル](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/whisper/index.html) |
| ONNX推論 | sherpa-onnx-coreに同梱される[ONNX Runtime / MIT](https://github.com/microsoft/onnxruntime) |
| 発話検出 | [Silero VAD / MIT](https://github.com/snakers4/silero-vad)のONNXモデル |
| 話者区間の分割 | [pyannote segmentation-3.0 / MIT](https://huggingface.co/pyannote/segmentation-3.0)のONNX変換 |
| 話者特徴量 | [3D-Speaker / Apache-2.0](https://github.com/modelscope/3D-Speaker)のERes2Netモデル |
| PCM WAV読取り | Python標準の[`wave`](https://docs.python.org/3.13/library/wave.html) |
| 波形処理・リサンプリング | [NumPy / BSD-3-Clause](https://numpy.org/doc/stable/license.html)、[SciPy / BSD-3-Clause](https://scipy.org/) |
| Windows音声録音 | [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch)とPortAudio |
| Windows画面状態 | [pywinauto](https://github.com/pywinauto/pywinauto) |
| TeamsのローカルCDP接続 | [websocket-client / Apache-2.0](https://github.com/websocket-client/websocket-client) |
| Outlookデスクトップ連携 | [pywin32](https://github.com/mhammond/pywin32) |
| Windows配布 | [PyInstaller](https://pyinstaller.org/)（実行環境をまとめたonedir形式） |
| Microsoft認証 | [MSAL Python](https://github.com/AzureAD/microsoft-authentication-library-for-python) |
| UI / API | [React](https://github.com/facebook/react)、[Vite](https://github.com/vitejs/vite)、[FastAPI](https://github.com/fastapi/fastapi) |

## 文字起こし・発話検出モデル

多言語Whisperの `tiny`、`base`、`small`、`medium`、`large-v3` に対応し、標準は `small` です。
sherpa-onnx向けに変換されたモデルの固定リビジョンから、int8のencoder・decoderとトークン表を取得します。
発話検出は公開のSilero VAD ONNXモデルを使用し、取得時にSHA-256を照合します。
実際の取得元は `giji/models.py` と、各モデルフォルダーの `sources.json` に記録します。

新しいモデルは `whisper-onnx-{size}` に保存します。旧版の `whisper-{size}` にあるCTranslate2用モデルとは
別の形式で、旧キャッシュを読み替えたり、旧エンジンへ切り替えたりはしません。

## 話者分離モデルの配布元

使用するモデルの組合せは、sherpa-onnxの[公式Python例](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/offline-speaker-diarization.py)と[モデルガイド](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html)に基づいています。

- 分割モデル: [sherpa-onnx-pyannote-segmentation-3-0.tar.bz2](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2)
- 特徴量モデル: [3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx)

これらは上流が公開する配布物で、アプリの取得手順にHugging Faceの個別トークンは不要です。
分割モデルのアーカイブに含まれるライセンスとREADMEはダウンロード時に保存します。
PyTorch版のpyannoteモデルを別途利用する場合、その配布元の利用条件やトークン要件は別です。

推論はローカルで行います。ポータブル版の標準モデルは同梱配置を直接読み込むため、
利用者によるHugging Faceへの登録・標準モデルの取得は不要です。別サイズのモデルを選ぶ場合は、
設定画面での明示的な取得が必要です。

ビルド時の収集処理は `scripts/collect_portable_licenses.py` にあります。依存物の版を変更して
再配布する際は、FFmpeg除外の検査とライセンス・通知・対応ソースの収集をやり直してください。
再配布時はZIP内のライセンスと対応ソースのフォルダーを省略しないでください。

## デザイン資料

- [Linear](https://linear.app/): 一覧と詳細を切り替えるナビゲーション、整理された状態表示の参考。
- [Granola](https://www.granola.ai/): 会議を起点にノートを読み返す情報配置の参考。

リンク先のブランド素材、画面画像、ソースコードをアプリ内に取り込んではいません。
