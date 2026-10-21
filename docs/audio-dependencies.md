# 音声ライブラリと配布物の確認

v0.5は、FFmpegを直接・間接に使わないことをアプリの構成条件としています。
旧構成のfaster-whisperはPyAV経由でFFmpegを利用するため、コマンドを呼ばない設定にするだけでなく、
エンジンと依存パッケージを入れ替えています。

**v0.2／v0.3の配布ZIPと、公開を中止したv0.4ビルドにはFFmpegが含まれます。FFmpeg禁止環境では使用しないでください。**
v0.4のGitタグや過去の成功したテストは、FFmpegを含まないことの証明ではありません。

## v0.5の処理経路

| 処理 | 使用する仕組み |
| --- | --- |
| 録音 | PyAudioWPatch / PortAudioによるマイクとWASAPIループバック。16bit PCM WAVへ保存 |
| WAVの読取り | Python標準の`wave`。圧縮音声への変換を挟まない |
| モノラル化・サンプルレート変換 | NumPy・SciPy |
| 発話検出 | sherpa-onnxとSilero VAD ONNX |
| 文字起こし | sherpa-onnx 1.13.8で多言語WhisperのONNXモデルをCPU実行 |
| 話者分離 | sherpa-onnxと分割・話者特徴量のONNXモデル |
| アプリ内の音声再生 | PCM WAVを範囲取得し、アプリで解析したサンプルをWeb AudioのPCMバッファへ渡す |

faster-whisper、PyAV、CTranslate2は新しい推論経路に使いません。FFmpegやffprobeを外部コマンドとして
呼び出したり、音声変換サービスへ送ったりもしません。再生では`<audio>`、`MediaSource`、
`decodeAudioData`によるメディアデコードを使いません。アプリが保存したPCM WAVを扱う構成であり、
任意のMP3・AAC・動画ファイルを取り込む機能はありません。

Whisperの区間時刻を使うため、単語単位の時刻は生成しません。話者は区間との重なりから割り当てます。
同じ認識区間の途中で話者が変わる場合は、境界の割り当てが粗くなることがあります。
エンジンとモデル形式を変更しているため、同じ録音を再処理しても旧版と同じ文章になるとは限りません。

## 採用ライブラリの確認

2026年10月21日に、PyPI公開情報と一致する以下のWindows x64用wheelを取得して調べました。

| ファイル | SHA-256 |
| --- | --- |
| `sherpa_onnx-1.13.8-cp313-cp313-win_amd64.whl` | `195455d8d6cc49f616e9d459b7d08f10a32daf2ddcbe90d4d479a8ba9aab28bf` |
| `sherpa_onnx_core-1.13.8-py3-none-win_amd64.whl` | `5579e80196d516e6dae23c8f629292ce3142ab8869925d32b94612fd86f93733` |

sherpa-onnxが宣言する依存は同版のsherpa-onnx-coreだけで、coreには追加のPythonパッケージ依存がありません。
両wheelのDLL・PYDのファイル一覧とPE importを確認し、参照先はPython・ONNX Runtime・Windows標準DLLでした。
FFmpegのDLL参照と、検査対象にしたFFmpeg識別子は見つかりませんでした。

上流のタグ`v1.13.8`はコミット`11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf`に対応します。
[Windows wheelのビルド定義](https://github.com/k2-fsa/sherpa-onnx/blob/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf/.github/workflows/build-wheels-win-x64.yaml)、
[CMakeのリンク定義](https://github.com/k2-fsa/sherpa-onnx/blob/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf/sherpa-onnx/csrc/CMakeLists.txt)、
[Windows版ONNX Runtimeの取得定義](https://github.com/k2-fsa/sherpa-onnx/blob/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf/cmake/onnxruntime-win-x64.cmake)と
本体のソース・ビルドファイルも確認し、FFmpegのビルド・リンク依存は見つかりませんでした。
これは固定版の配布物と上流構成の確認であり、上流wheelの再現ビルドを実施した記録ではありません。
依存物を更新した場合や、最終ZIPについては別途検査が必要です。

## 配布物の検査範囲

ビルド時には、インストール済みパッケージの依存情報、配布フォルダー、DLL等のネイティブバイナリ、
同梱ソースと最終ZIPを検査します。FFmpeg本体だけでなく、PyAV等のラッパー、FFmpeg系のファイル名、
DLL参照・識別子を禁止対象に含め、検出時は公開用のビルドを失敗させる構成です。

検査記録は配布フォルダーの次のファイルに含めます。

- `DEPENDENCY-ENVIRONMENT.json`: ビルド環境の依存パッケージと、ネイティブファイルのハッシュ・DLL参照。
- `DEPENDENCY-AUDIT.json`: 配布フォルダーと入れ子のソースアーカイブを検査した結果、ネイティブファイルのハッシュ・通常および遅延DLL参照。

最終ZIPを作成した後と、検証済みZIPをReleaseへ公開する前にも、アーカイブ内の検査を実行します。
検査結果は対象のコミット・依存バージョン・配布物ハッシュと対応付けます。
実行済みの検査・Windowsでの起動確認は [検証記録](verification.md) を確認してください。
この文書の処理経路の説明だけを、未検査の独自ビルドや別バージョンの検査結果として扱わないでください。

対象は本アプリが配布するファイルと呼び出す音声処理です。利用者のPCに既にあるTeams・ブラウザーなど、
別製品の内部構成をこのアプリが検査・変更するものではありません。ブラウザー自体は配布ZIPに同梱しません。

## 旧版からの移行

配布ZIPは旧フォルダーへ上書きせず、別の空フォルダーにすべて展開します。上書きでは、旧版のPyAVや
FFmpegのDLLが残るためです。録音・文字起こし・議事録・設定の保存先は変わらず、旧版のPCM WAVも読み取れます。

ソース版は旧`.venv`を使い回さず、新しい仮想環境へセットアップしてください。旧版の禁止対象パッケージが
残っている場合、セットアップは`pip`による更新前に停止し、利用者の環境を勝手に削除・変更しません。`pip install`で依存定義を
更新しても、不要になったパッケージは自動で削除されません。会社の管理手順に従って旧環境を整理し、
録音や議事録の保存先を開発環境と取り違えて削除しないようにします。

モデルは新しい `whisper-onnx-{size}` フォルダーを使います。旧`whisper-*`のCTranslate2用モデルは読み込みません。
同梱版の標準`small`は追加取得不要です。ソース版や別サイズを選ぶ場合は、設定からONNXモデルを取得してください。
`large-v3-turbo`はv0.5の対応モデルではありません。旧設定に残っている場合は、対応するモデルを選び直してください。

## 開発環境で検査を再実行する

現在のソースとクリーンな開発環境を使います。配布版の通常利用にこの操作は不要です。

```powershell
.\.venv\Scripts\python.exe scripts/audit_dependencies.py --environment
.\.venv\Scripts\python.exe scripts/audit_dependencies.py --archive dist/giji-automemo-windows-x64.zip
```

検査の実装は[`scripts/audit_dependencies.py`](../scripts/audit_dependencies.py)です。未知の依存物を追加したときは、
自動検査の結果だけでなく、その配布物・上流のビルド構成も確認してから再配布してください。
