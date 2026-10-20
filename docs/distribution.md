# Windows ポータブル版

`giji-automemo-windows-x64.zip` を展開し、`GijiAutoMemo/GijiAutoMemo.exe` をダブルクリックします。黒い起動ウィンドウと、既定ブラウザーのアプリ画面が開きます。Python・Node.js・Gitのインストールやセットアップコマンドは不要です。

Windows 11 x64、既存の Teams、サインイン済みの **クラシック Outlook** を使います。新しい Outlook はこの COM 接続に対応していません。Outlook予定表の取得には、アプリ登録や追加の Microsoft API キーは不要です。

初回は設定画面で録音デバイス、録音の同意、LLM の URL・モデル名・API キー、文字起こしを LLM に送る同意を設定してください。録音の同意前に自動録音は始まりません。Teams 参加者への説明や所属組織の録音ルールも確認してください。文字起こし・話者分離は PC 内で動作し、API キーは Windows 資格情報マネージャーに保存されます。

アプリを終了するときは起動ウィンドウで `Ctrl+C` を押します。ブラウザーのタブを閉じても監視は続きます。タブをもう一度開くには同じ EXE を起動してください。録音中は PC をスリープさせず、アプリの起動ウィンドウも閉じないでください。

## 同梱内容と保存先

| 場所 | 内容 |
| --- | --- |
| `GijiAutoMemo.exe` | 起動プログラム |
| `_internal/` | Python、ライブラリ、DLL、ビルド済み画面 |
| `models/whisper-small/` | ローカル文字起こしモデル |
| `models/diarization/` | 話者分離モデル |
| `MODEL-MANIFEST.json` | 同梱モデルの SHA-256 とファイルサイズ |
| `licenses/` | 第三者のライセンス・著作権表示・依存バージョン |
| `third-party-sources/` | 同梱コンポーネントの対応ソースと取得元 |

フォルダーをまとめて保持してください。EXE だけの移動では動きません。標準の `small` モデルを使う場合、初回起動時のモデル取得は不要です。設定で別の文字起こしモデルに変更した場合は、そのモデルのダウンロードが必要です。

録音・文字起こし・議事録・設定は `%LOCALAPPDATA%\giji-automemo` に保存します。配布フォルダーは別の場所へ移動できます。アップデート時は旧アプリを終了し、新しい ZIP を別のフォルダーへ展開してください。同じ Windows ユーザーでは保存済み会議を引き続き参照します。配布 ZIP に個人の録音・予定・API キーは含みません。

## 動作と費用の範囲

アプリ、標準モデル、ローカル録音・文字起こし・話者分離は追加のサービス利用料なしで動作します。Teams・Outlook は既存の利用環境を使い、LLM の呼び出しは契約先 API の料金が発生する場合があります。予定表の同期と議事録の生成には必要な外部サービスへの通信が発生します。モデルの処理には PC のメモリー・CPU・保存容量を使い、CPU の速度や会議の長さによって生成時間が変わります。

Teams の開始・終了は Windows UI Automation で検出します。Teams の表示やバージョン変更によって検出できない場合があるため、初回に短い実会議で自動開始・終了を確認してください。検出できない場合は手動開始・終了を使えます。予定終了時刻だけでは録音を止めず、実際の退出を検出した後の猶予時間を置いて停止するため、延長にも対応します。

話者ラベルは声の特徴で分けた推定結果で、本人確認ではありません。相手の実名はラベルに手動で対応付けます。文字起こし・話者分離・議事録には誤りが含まれる場合があるので、共有前に確認してください。

## 配布ファイルの確認

Release の `portable-SHA256SUMS.txt` と以下の出力を比較できます。

```powershell
Get-FileHash .\giji-automemo-windows-x64.zip -Algorithm SHA256
```

この無料配布版はコード署名証明書を付けていません。Windows の警告が出た場合は取得元の GitHub Release とハッシュを確認し、組織のポリシーに従ってください。セキュリティ機能を無効にする必要はありません。

## 開発者向けの再ビルド

Windows x64、Python 3.11 x64、Node.js 22、Git、Visual Studio の C++ Build Tools を使います。Visual C++ の再頒布用 DLL もアプリに同梱するため、利用者側で C++ ランタイムを別途セットアップする必要はありません。ソースのルートで次を実行します。ビルド時はモデルと対応ソースを取得するためネット接続が必要です。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,windows,local]" "av==16.1.0" "pyinstaller>=6.16,<7"
.\.venv\Scripts\python.exe -m pytest -q
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-portable.ps1 -Python .\.venv\Scripts\python.exe
```

`dist/giji-automemo-windows-x64.zip` が完成品です。`Windows portable release` ワークフローも同じ手順を使い、`ref` で指定したコミットをチェックアウトして、テスト・モデル同梱・ライセンス収集・ZIP 検証・ZIP を展開した実 EXE の推論と HTTP 起動試験を行います。成功した ZIP は Actions artifact に保存します。任意の `release_tag` を指定した場合は、タグとビルドのコミットが一致することを確認し、既存の GitHub Release にも添付します。

起動試験はホストされた Windows ランナーで、Whisper `small` の CPU 推論、話者分離 ONNX、VAD、音声 DLL、UI Automation ライブラリ、アプリ画面と認証付き API を確認します。ランナーには実際の Teams 会議・Outlook アカウント・ヘッドセットがないため、それらとの接続と録音品質は利用する PC での確認が必要です。無音テストは日本語認識精度の評価ではありません。

## ライセンス

アプリ自身のソースは MIT です。同梱 Python、モデル、依存ライブラリにはそれぞれのライセンスが適用されます。特に PyAV の Windows wheel が含む FFmpeg には GPL のコンポーネントが含まれるため、配布 ZIP 全体を単一の MIT ライセンスとして扱わないでください。ライセンス本文・著作権表示・取得元・対応ソースは ZIP 内の `licenses/` と `third-party-sources/` に同梱します。再配布する場合はこれらも保持してください。

配布方式は [PyInstaller の one-folder 構成](https://pyinstaller.org/en/stable/operating-mode.html)を使います。実行時のファイル配置は [PyInstaller のランタイム仕様](https://pyinstaller.org/en/stable/runtime-information.html)、FFmpeg の構成は [PyAV の公式ビルド](https://github.com/PyAV-Org/pyav-ffmpeg/tree/8.0.1-3)を参照しています。
