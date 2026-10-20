# 検証結果

v0.2.0。実装済みと実機検証済みを区別して記載しています。

## この開発環境で確認したこと

- Pythonの自動テスト162件成功：従来版Outlookの予定読み取り、定期予定・開催回の変更・取消、
  Windows地域設定・UTC変換・日付境界・COMのタイムアウト、Graphのページング・認証取消、
  取得元の切替・旧キャッシュの除去、録音の保存と時刻同期、
  会議の延長・短縮・再接続、中断復旧、話者割当、LLMエラー、設定・秘密情報の保護。
- ローカルAPI：起動時トークン、Host / Origin検証、音声専用署名URL、Range再生、
  セッション削除・Markdown出力、外部保存先の拒否。
- Ruff静的検査、TypeScriptチェック、Vite本番ビルド。
- Chromiumで実バックエンドを使用：ホーム、デモ議事録、文字起こし、話者名変更、
  Markdownダウンロード、設定保存、デモ削除、390px幅での表示。JavaScript例外なし。
- v0.2のブラウザー確認：既定のOutlook連携に登録・サインイン要求が出ないこと、
  未接続でも同期を開始できること、7項目の初回チェックリスト、Graphとの明示切替、
  設定の保存・空状態・390px幅での表示。更新した画面でもJavaScript例外なし。
- npm audit：検証時点で既知の脆弱性0件。
- 実音声モデル：faster-whisper tiny（CPU int8）とsherpa-onnxを実際にダウンロードし、
  公開英語音声の文字起こし・話者クラスタリング・発話ラベル割当を実行。
  無音マイクのトラックで誤った発話を生成しないことも確認。
- 音声デコードの実行テストでPyAV 19との非互換を確認し、動作したPyAV 16系を含む
  `av>=14,<17` に依存範囲を制限。
- 完成済みユーザーモデルと同梱モデルの優先順位、不完全なモデルの検出、
  同梱モデルをコピー・ダウンロードせずに使う経路を検証。
  同梱配置を指定した実モデル推論でも、利用者用モデルフォルダーが作成されないことを確認。

自動テストではMicrosoft・LLMのHTTP応答とWindowsのデバイスをモックしています。
実モデルの推論検証はLinux上の短いサンプルで、長時間・日本語・複数話者の精度を
保証する測定ではありません。

## Windows同梱版で確認したこと

ソース `7f8769b34971f86b8d8edc22e95e84e64ee4862d` の
[Windows/Linux CI](https://github.com/kitworks-iino/giji-automemo/actions/runs/39473006070) と
[Windowsポータブルビルド](https://github.com/kitworks-iino/giji-automemo/actions/runs/39473008565)
が成功しました。

- Windows上でPythonテスト162件、画面ビルド、EXE生成、モデル・ライセンス・対応ソースの同梱。
- 作成したZIPを別の一時フォルダーへ展開し、そのEXEで検証。
- 同梱Whisper smallのCPU int8推論、VAD、話者分離ONNX、PyAV音声デコードを実行。
  モデル取得をオフライン設定にし、利用者用モデルフォルダーが作られないことを確認。
- Windows UI Automation、Outlook連携用COMライブラリ、資格情報バックエンドのロード。
- 同梱VC++ DLL 11個を配置し、必要なCRT DLLが配布フォルダーから読み込まれることを確認。
- 認証付きローカルAPI、デモデータ、同梱モデルの準備状態、ビルド済み画面・静的ファイルを確認。
- ZIPの整合性とSHA-256を検証。配布ZIPは835,797,587 bytes。

Windowsでのモデル検証は無音サンプルを使う依存関係・実行確認で、日本語認識や
複数話者の精度を評価するものではありません。Hosted runnerにOutlookアカウント、
実際のTeams会議、マイク・ヘッドセットはありません。

## 利用するWindows PCで必要な確認

- 利用中の従来版Outlookのプロファイルで、予定・定期開催回・変更・取消を取得できること。
  Object Model Guardの確認や会社の制限の有無も実機確認が必要です。
- Windows 11、利用中のTeams版、ヘッドセットでのUIA検知とWASAPI録音。
- Teamsを最小化した場合、会議の延長・短縮、退出・再参加の実動作。
- 使用するLLM APIキー・モデルでの議事録生成。
- 使用する会社PCで、未署名の配布EXEを起動できること。
- 任意のGraph連携を選ぶ場合だけ、Microsoft 365テナントの認証・権限・予定取得。

実アカウント・Windows端末・ヘッドセット・実APIキーはこの開発環境にないため、
上記の実機検証は行っていません。手順は[実機確認項目](acceptance.md)にあります。
GitHub ActionsのWindowsジョブもハードウェアやTeamsの実会議を検証するものではありません。

## 再実行

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
cd frontend
npm ci
npm run build
npm audit
```
