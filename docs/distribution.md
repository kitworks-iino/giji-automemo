# Windows ポータブル版

`giji-automemo-windows-x64.zip` を展開し、`GijiAutoMemo/GijiAutoMemo.exe` をダブルクリックします。黒い起動ウィンドウと、既定ブラウザーのアプリ画面が開きます。Python・Node.js・Gitのインストールやセットアップコマンドは不要です。

Windows 11 x64と、サインイン済みの **Teamsデスクトップ** を使います。v0.5の標準取得元はTeamsで、Outlookは不要です。アプリ登録や追加のMicrosoft APIキーも不要です。予定連携はTeamsのカレンダー表示を読み取るプレビューで、実際のTeamsアカウントを使うWindows実機での取得は未検証です。[接続方法と取得範囲](https://github.com/kitworks-iino/giji-automemo/blob/main/docs/teams-desktop.md)を確認してください。

標準方式はWindows UI Automationです。最初にTeamsのカレンダーを利用者が今日の日表示にして読み取ります。確認後は最小化できますが、トレイ格納を含め常に読み取れることは保証していません。Teamsがカレンダー情報を破棄・休止した場合は取得できず、予定の読取り処理がTeamsを復元・前面化することもありません。通常は管理者権限・Teams再起動・デバッグ接続の設定は不要で、アプリがカレンダーや参加ボタンを自動クリックすることもありません。v0.2で保存したOutlookなどの取得元は更新後も保持するため、Teamsを使う場合は設定を変更して保存してください。

任意のCDP方式を選んだ場合だけ、接続準備とTeamsの完全終了・再起動が必要です。この準備はアプリやTeamsの再起動後にも必要です。利用後は連携を解除し、会議終了後にTeamsを完全終了して通常起動してください。一時設定を復元しただけでは、起動中のTeamsのデバッグポートは閉じません。

初回は設定画面で録音デバイス、録音の同意、LLM の URL・モデル名・API キー、文字起こしを LLM に送る同意を設定してください。録音の同意前に自動録音は始まりません。Teams 参加者への説明や所属組織の録音ルールも確認してください。文字起こし・話者分離は PC 内で動作し、API キーは Windows 資格情報マネージャーに保存されます。

アプリを終了するときは起動ウィンドウで `Ctrl+C` を押します。ブラウザーのタブを閉じても監視は続きます。タブをもう一度開くには同じ EXE を起動してください。録音中は PC をスリープさせず、アプリの起動ウィンドウも閉じないでください。

## 同梱内容と保存先

| 場所 | 内容 |
| --- | --- |
| `GijiAutoMemo.exe` | 起動プログラム |
| `_internal/` | Python 3.13、ライブラリ、DLL、ビルド済み画面 |
| `models/whisper-onnx-small/` | ONNX版Whisper small、発話検出モデル、トークン表 |
| `models/diarization/` | 話者分離モデル |
| `MODEL-MANIFEST.json` | 同梱モデルのSHA-256とファイルサイズ |
| `DEPENDENCY-ENVIRONMENT.json` | ビルド環境の依存一覧・ネイティブファイルのハッシュとDLL参照 |
| `DEPENDENCY-AUDIT.json` | 配布フォルダーと同梱アーカイブの依存検査記録 |
| `licenses/` | 第三者のライセンス・著作権表示・依存バージョン |
| `third-party-sources/` | 同梱コンポーネントの対応ソースと取得元 |

フォルダーをまとめて保持してください。EXE だけの移動では動きません。標準の `small` モデルを使う場合、初回起動時のモデル取得は不要です。設定で別の文字起こしモデルに変更した場合は、そのモデルのダウンロードが必要です。

v0.2／v0.3のZIPと未公開のv0.4ビルドにはFFmpegが含まれます。FFmpeg禁止環境では旧版を使用せず、v0.5の配布物と検査記録を使用してください。旧配布フォルダーへ上書きすると、使わなくなったDLLが残るため、必ず別の空フォルダーへ展開します。

録音・文字起こし・議事録・設定は `%LOCALAPPDATA%\giji-automemo` に保存します。配布フォルダーは別の場所へ移動できます。アップデート時は旧アプリを終了し、新しい ZIP を別のフォルダーへ展開してください。同じ Windows ユーザーでは保存済み会議を引き続き参照します。配布 ZIP に個人の録音・予定・API キーは含みません。

## 動作と費用の範囲

アプリ、標準モデル、ローカル録音・文字起こし・話者分離は追加のサービス利用料なしで動作します。Teamsは既存の利用環境を使い、LLMの呼び出しは契約先APIの料金が発生する場合があります。標準の予定連携はPC内のTeamsのカレンダー表示を読み取り、予定の最新情報はTeams自身が受信します。アプリは標準の連携でGraphや非公開の予定APIへ接続せず、Microsoftの認証情報も抽出しません。議事録を生成するときは、同意した文字起こしを設定したLLMサービスへ送信します。モデルの処理にはPCのメモリー・CPU・保存容量を使い、CPUの速度や会議の長さによって生成時間が変わります。

Teamsが読み込んだカレンダー情報から確認できる予定だけが対象です。読み取りに失敗すると取得状態にエラーを示し、保存済み予定を残します。最終読取り時刻は画面情報を読めた時刻で、Microsoft側の最新データとの同期完了を示しません。標準方式はWindowsのタイムゾーンを使うため、Teams側の設定との一致を確認してください。画面外の予定や仮想スクロールなどにより一部しか取得できない場合があるため、完全な予定一覧が取得できたとは保証しません。Outlook・Graphは詳細設定で明示的に選ぶ代替手段で、Teamsの取得失敗から勝手に利用することはありません。

Teams の開始・終了は Windows UI Automation で検出します。Teams の表示やバージョン変更によって検出できない場合があるため、初回に短い実会議で自動開始・終了を確認してください。検出できない場合は手動開始・終了を使えます。予定終了時刻だけでは録音を止めず、実際の退出を検出した後の猶予時間を置いて停止するため、延長にも対応します。

話者ラベルは声の特徴で分けた推定結果で、本人確認ではありません。相手の実名はラベルに手動で対応付けます。文字起こし・話者分離・議事録には誤りが含まれる場合があるので、共有前に確認してください。

## 配布ファイルの確認

Release の `portable-SHA256SUMS.txt` と以下の出力を比較できます。

```powershell
Get-FileHash .\giji-automemo-windows-x64.zip -Algorithm SHA256
```

ZIP内の`DEPENDENCY-ENVIRONMENT.json`と`DEPENDENCY-AUDIT.json`には、対象依存物・ファイルのハッシュ・DLL参照を記録します。モデルは`MODEL-MANIFEST.json`で確認できます。利用者がこれらを読むためにPythonを追加する必要はありません。[検査の対象と再実行](audio-dependencies.md)

この無料配布版はコード署名証明書を付けていません。Windows の警告が出た場合は取得元の GitHub Release とハッシュを確認し、組織のポリシーに従ってください。セキュリティ機能を無効にする必要はありません。

## 開発者向けの再ビルド

既存のv0.4以前の仮想環境や配布フォルダーへ上書きせず、クリーンな環境でビルドします。`pip install`だけでは旧PyAVやFFmpegのDLLを削除できません。

Windows x64、Python 3.13 x64、Node.js 22、Git、Visual Studio の C++ Build Tools を使います。ソース版の `setup.ps1` は3.13を優先し、3.12・3.11へ順に切り替えられますが、v0.5の同梱版は3.13でビルドします。CIのPython対象は3.11と3.13です。Visual C++ の再頒布用 DLL もアプリに同梱するため、利用者側で C++ ランタイムを別途セットアップする必要はありません。ソースのルートで次を実行します。ビルド時はモデルと対応ソースを取得するためネット接続が必要です。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,windows,local]" "pyinstaller>=6.16,<7"
.\.venv\Scripts\python.exe -m pytest -q
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-portable.ps1 -Python .\.venv\Scripts\python.exe
```

`dist/giji-automemo-windows-x64.zip` が完成品です。`Windows portable release` ワークフローも同じ手順を使い、`ref` で指定したコミットをチェックアウトして、テスト・モデル同梱・ライセンス収集・禁止依存物の検査・ZIP検証・ZIPを展開した実EXEの推論とHTTP起動試験を行います。成功した ZIP は Actions artifact に保存します。任意の `release_tag` を指定した場合は、タグとビルドのコミットが一致することを確認し、既存の GitHub Release にも添付します。

検証済みの ZIP を再ビルドせず公開する場合は、`Publish verified Windows portable` に成功したビルドの `run_id` と既存の Release の `tag` を指定します。ワークフローの出所、タグのコミット、成果物の SHA-256、ZIP 内のソースコミットが一致することを確認してから、ダウンロードしたZIPの禁止依存物検査も再実行し、同じファイルをReleaseに添付します。

v0.5の検証完了状況は [検証記録](https://github.com/kitworks-iino/giji-automemo/blob/main/docs/verification.md) を確認してください。旧版の成功結果をv0.5の確認済み事項として読み替えないでください。

起動試験はホストされたWindowsランナーで、ONNX版Whisper `small`のCPU推論、話者分離ONNX、VAD、録音ライブラリ、UI Automationライブラリ、アプリ画面と認証付きAPIを確認します。ランナーには実際のTeams会議・予定表・ヘッドセットがないため、Teamsからの予定取得、会議への参加・退出検知、録音品質は利用するPCでの確認が必要です。テスト用の画面データや無音テストは、実際のTeamsへの接続確認や日本語認識精度の評価ではありません。

## ライセンス

アプリ自身のソースは MIT です。同梱 Python、モデル、依存ライブラリにはそれぞれのライセンスが適用されます。v0.5ではPyAV・FFmpegを同梱しませんが、配布ZIP全体を単一のMITライセンスとして扱うことはできません。ライセンス本文・著作権表示・取得元・対応ソースは ZIP 内の `licenses/` と `third-party-sources/` に同梱します。再配布する場合はこれらも保持してください。

配布方式は [PyInstaller の one-folder 構成](https://pyinstaller.org/en/stable/operating-mode.html)を使います。実行時のファイル配置は [PyInstaller のランタイム仕様](https://pyinstaller.org/en/stable/runtime-information.html)を参照してください。音声処理とFFmpeg除外の確認範囲は [音声ライブラリと配布物の確認](audio-dependencies.md) に記載しています。
