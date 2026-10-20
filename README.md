# Giji AutoMemo

**会議に集中する。記録は、あとから整う。**

Windows 11で動く、Teams会議の録音・文字起こし・議事録生成アプリです。Outlookの予定をホーム画面へ同期し、自分と相手の音声をPC内に保存。会議からの退出を検知すると、ローカル音声モデルと手持ちのLLM APIで日本語の議事録を作ります。

録音・文字起こし・話者分離の従量料金、クラウドサーバー代、アプリの利用料はありません。**LLM APIの利用料は契約先の料金に従います。** 既存のTeams/Outlookアカウント、PC、ネットワーク回線は必要です。

> 自動処理には初回設定とアプリの常駐が必要です。Teamsの参加/退出はWindowsの画面要素から推定するため、あらゆるTeams更新・端末での検知を保証するものではありません。手動録音・停止も利用できます。

![ホーム画面（デモデータ）](docs/screenshots/home.png)

[議事録画面](docs/screenshots/minutes.png) · [設定画面](docs/screenshots/settings.png) · [検証結果](docs/verification.md)

## できること

| 機能 | 実装 |
| --- | --- |
| 今日のTeams会議 | Microsoft GraphでOutlook予定を定期同期。繰り返し予定・変更・取消にも対応 |
| 自分と相手の録音 | 選択したマイクと再生デバイスのWASAPIループバックを別々のWAVへ保存 |
| 自動開始・終了 | Teamsデスクトップの会議操作ボタンを監視。予定終了時刻では録音を切らない |
| 文字起こし | faster-whisperをPC内で実行。日本語を標準設定 |
| 話者分離 | sherpa-onnxで相手側の音声をクラスタリング。自分のマイク音声は別ラベル |
| 議事録 | LLMで要約、決定事項、担当・期限つきToDo、議題、未解決事項を生成 |
| ホーム画面 | 予定、録音時間、音量、処理段階、議事録一覧、接続状態を表示 |
| 振り返り | 音声再生、話者名の編集、Markdown出力、失敗時の再処理 |
| デモ | 実際の録音・API接続を使わず、明示されたサンプルデータで画面を確認 |

**文字起こしは録音終了後に行います。** 会議中のリアルタイム字幕ではありません。録音停止後、文字起こし → 話者分離 → 議事録生成の順に進み、画面で進行状況を確認できます。

## まず起動する

推奨環境は **Windows 11 x64 / Python 3.11 x64 / Node.js 22.12以上 / メモリ16GB**。GPUは不要です。初回の依存パッケージとモデルの取得にネット接続・数GBの空き容量が必要です。処理速度は会議の長さとPC性能に依存します。

**画面のビルドを省略する場合**は、[リリースのZIP](https://github.com/kitworks-iino/giji-automemo/releases/latest)をダウンロード・展開し、Python 3.11 x64をインストールした上で以下を実行してください。このZIPではNode.js・Gitは不要です。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -SkipFrontend
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

ソースからビルドする場合：

1. [Python](https://www.python.org/downloads/windows/)、[Node.js](https://nodejs.org/en/download)、[Git](https://git-scm.com/download/win)をインストールします。
2. PowerShellで実行します。

```powershell
git clone https://github.com/kitworks-iino/giji-automemo.git
cd giji-automemo
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

`setup.ps1` はプロジェクト専用の `.venv` を作り、Python依存関係と画面をビルドします。実行ポリシーの指定はこのPowerShellプロセス限りで、Windowsの恒久設定を変更しません。会社のポリシーで実行が制限されている場合は管理者に相談してください。

起動したブラウザーがアプリのホーム画面です。起動用ターミナルは開いたままにしてください。ブラウザーのタブを閉じてもバックエンドが動いていれば処理は続きます。終了はターミナルで `Ctrl+C`。PCのスリープ中・電源OFF中は録音できません。

初回はデモデータで表示を確認してから、設定画面で以下を設定してください。

1. Microsoft連携のクライアントID・テナントIDを保存してサインイン。
2. Teamsで使用しているマイクと同じ再生デバイスを選択。
3. 音声モデルをダウンロード。
4. LLMのAPIキー、モデル名、APIベースURLを設定。
5. 録音と文字起こしの外部送信について設定画面で確認し、自動録音を有効化。

Microsoft連携には、あなたの組織のEntraアプリ登録が必要です。**APIキーだけではOutlookに接続できません。** 手順は [Microsoft連携の設定](docs/microsoft-setup.md) にまとめています。

## 普段の使い方

アプリを起動しておくと、当日の予定が定期的に更新されます。会議カードの参加操作でTeamsを開き、Teams側で会議に参加します。アプリが参加状態を検知すると録音が始まります。ホームの録音表示と2つの音量メーターを確認してください。

会議が延長しても予定時刻では停止しません。早く終わったときは退出検知後の短い猶予を経て生成を始めます。予定時刻では実際の終了を判断できないため、退出状態を使います。手動で開始した録音もTeams参加を検知済みなら退出に連動します。検知できない環境では手動で停止してください。

Teamsの検知が不明になった場合や録音デバイスに問題がある場合は、画面に状態を表示します。検知不能を終了と決めつけず、手動停止を利用できます。会議の対応付けが曖昧な場合は、会議カードから参加するか手動で対象を選びます。

生成後はホームから録音を開き、議事録・文字起こし・音声を確認できます。相手のラベルを実名に置き換えたい場合は話者名を編集してください。**声だけから実名を推測する機能ではありません。**

## データと費用

- 音声、文字起こし、議事録、設定はPC内に保存します。APIキーとMicrosoft認証情報にはOSの資格情報ストアを使用します。
- 音声認識と話者分離はローカル処理です。モデル取得後の推論にインターネットは不要です。
- Outlook同期・Microsoftサインインと、クラウドLLMによる議事録生成には接続が必要です。
- 外部LLMへ送るのは文字起こしと議事録生成に必要なテキストです。録音ファイルは送信しません。送信先は設定したAPIベースURLです。
- LLM APIキーが未設定でも録音・ローカル文字起こしは利用できます。LLMでの議事録生成にはキーと送信設定が必要です。
- 長い会議は分割して要約します。LLM APIには従量料金が発生し得ます。組織が許可したプロバイダーを設定してください。
- 録音と生成物は自動削除しません。不要なセッションは画面から削除し、必要なものはMarkdownで書き出してください。

保存先・復旧・トラブル時の確認は [運用ガイド](docs/operations.md) を参照してください。

## 知っておきたい制約

- Windows 11のTeamsデスクトップ版を対象にしています。ブラウザー版Teams、macOS、Linuxの自動録音は対応対象外です。
- 無料構成を維持するため、Teams会議にボットを入れたり、Teams Premiumの文字起こしを取得したりしません。Teams標準の録音通知は出ないため、録音開始前に参加者への案内と必要な同意を済ませてください。
- 相手側は再生デバイス全体の音です。同じデバイスで鳴る通知音・動画音も入ります。スピーカー音をマイクが拾う場合は二重記録になるためヘッドセットを推奨します。
- Teamsでミュートしても、独立したマイク録音にはローカルの声が入り得ます。不要な会話はアプリ側で停止するか、マイク自体をミュートしてください。
- 音声認識・話者分離・LLMには誤りがあります。同時発話、短い発言、似た声、雑音では話者の誤割当が起きます。担当者や決定事項は原文・音声と照合してください。
- 自動検知はWindows UI Automationを利用する推定です。Teamsの画面構造変更、アクセシビリティ情報不足、スリープ、デバイス切断の影響を受けます。
- 組織のEntra登録・同意・条件付きアクセスポリシーによってMicrosoft連携が許可されない場合があります。管理者権限を回避する機能はありません。

## 開発

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
cd frontend
npm ci
npm run build
```

構成は Python/FastAPI + SQLite + React/TypeScript/Vite。ローカルAPIはループバックに限定し、起動ごとのトークンで保護します。[実装の構成](docs/architecture.md) と [実機確認項目](docs/acceptance.md) も参照してください。

GitHub ActionsでUbuntu/Windowsのテストと画面ビルドを行います。`Source package` ワークフローを手動実行すると、ビルド済み画面を含むソースZIPを成果物として取得できます。**Windows用の署名済みEXEではありません**。Python依存関係のセットアップは必要です。ZIP版は `setup.ps1 -SkipFrontend` でセットアップでき、Node.jsは不要です。

## デザインの参考

[Linear](https://linear.app/) の整理されたナビゲーションと状態表示、[Granola](https://www.granola.ai/) の会議とノートを中心にした画面を参考に、独自の日本語UIにしています。両サービスのコード・ロゴ・画像は使用していません。

## ライセンス

アプリのソースは [MIT](LICENSE)。依存パッケージと音声モデルはそれぞれのライセンスに従います。[主な依存物・モデル](docs/third-party.md) を参照してください。モデルやAPIキー、録音データはこのリポジトリに含めません。
