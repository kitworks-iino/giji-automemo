# 主な依存物・モデル

このアプリのMITライセンスは、依存パッケージやモデルのライセンスを置き換えません。モデルの重みはリポジトリに同梱せず、設定画面での明示的な操作により上流からダウンロードします。

| 用途 | プロジェクト・資料 |
| --- | --- |
| ローカル文字起こし | [faster-whisper / MIT](https://github.com/SYSTRAN/faster-whisper) |
| Whisper重み | [OpenAI Whisper / MIT](https://github.com/openai/whisper) を基にしたfaster-whisper対応モデル |
| ローカル話者分離 | [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) |
| 話者区間の分割 | [pyannote segmentation-3.0 / MIT](https://huggingface.co/pyannote/segmentation-3.0) のONNX変換 |
| 話者特徴量 | [3D-Speaker / Apache-2.0](https://github.com/modelscope/3D-Speaker) のERes2Netモデル |
| Windows音声録音 | [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) |
| Windows画面状態 | [pywinauto](https://github.com/pywinauto/pywinauto) |
| Microsoft認証 | [MSAL Python](https://github.com/AzureAD/microsoft-authentication-library-for-python) |
| UI / API | [React](https://github.com/facebook/react)、[Vite](https://github.com/vitejs/vite)、[FastAPI](https://github.com/fastapi/fastapi) |

## 話者分離モデルの配布元

使用するモデルの組合せは、sherpa-onnxの[公式Python例](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/offline-speaker-diarization.py)と[モデルガイド](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html)に基づいています。

- 分割モデル: [sherpa-onnx-pyannote-segmentation-3-0.tar.bz2](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2)
- 特徴量モデル: [3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx)

これらは上流が公開するGitHub Releaseの配布物で、アプリの取得手順にHugging Faceの個別トークンは不要です。分割モデルのアーカイブに含まれるライセンスとREADMEはダウンロード時に保存します。PyTorch版のpyannoteモデルを別途利用する場合、その配布元の利用条件やトークン要件は別です。

推論はローカルで行います。ダウンロード先の可用性やモデルの再配布条件は上流に依存するため、モデルを組み込んで再配布する場合は、取得したファイルのライセンスを確認してください。

## デザイン資料

- [Linear](https://linear.app/): 一覧と詳細を切り替えるナビゲーション、整理された状態表示の参考。
- [Granola](https://www.granola.ai/): 会議を起点にノートを読み返す情報配置の参考。

リンク先のブランド素材、画面画像、ソースコードをアプリ内に取り込んではいません。
