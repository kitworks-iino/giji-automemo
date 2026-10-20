# Outlook連携の設定

標準設定は、このPCにある **Outlook（クラシック）** の予定表を読み取る方式です。**Microsoft Entraへのアプリ登録、クライアントID、テナントID、Microsoft用APIキーは不要です。** 普段使っているOutlookのプロファイルを利用します。

## 最初に確認すること

1. Outlook（クラシック）を起動し、いつものアカウントでサインインします。
2. そのOutlookの既定の予定表に、今日のTeams会議が表示されていることを確認します。
3. Giji AutoMemoをOutlookと同じWindowsユーザーで起動します。
4. 設定の予定連携で **Outlook デスクトップ** を選び、**Outlookの予定を同期** を押します。
5. 接続状態とホームの会議一覧を確認します。以後はアプリ起動中、標準60秒ごとに同期します。

LLM APIキーは議事録生成用であり、Outlookの予定取得には使いません。アプリがOutlookのパスワードやアクセストークンを抽出することもありません。

## 対応するOutlookと予定表

**「新しいOutlook」やブラウザー版Outlookは、このデスクトップ連携の対象外です。** クラシック版のOutlook Object Modelを使うため、PCにOutlookが入っているだけでなく、クラシック版でプロファイルが利用できる必要があります。Outlookを開いて予定を確認できる状態にしてから同期してください。

取得対象は、既定予定表の当日に重なるTeams会議です。繰り返し予定を各回に展開し、Windowsのローカル日付を基準に取得します。別アカウント・共有予定表・グループ予定表だけにある会議は表示されない場合があります。チャネル会議が個人予定表に入る条件も通常の招待と異なります。[Teamsの予定表とチャネル会議](https://support.microsoft.com/en-us/teams/meetings/see-all-your-meetings-in-microsoft-teams)

Outlookがオフラインの場合、見えるのはOutlookが保持している予定です。最新の変更を反映するにはOutlook側の同期も必要です。

## 読み取りの確認やエラーが出たとき

Outlookは、外部アプリによる本文・主催者・参加者情報などの読み取りをObject Model Guardで保護しています。端末や組織の設定により確認画面が表示されたり、読み取りが拒否されたりすることがあります。アプリは保護設定や組織のポリシーを変更しません。[Microsoftの保護対象一覧](https://learn.microsoft.com/en-us/office/vba/outlook/how-to/security/protected-properties-and-methods)

予定取得は別の補助プロセスで実行します。Outlookの応答待ちが長くてもアプリ全体を停止させず、同期状態に問題を表示します。確認画面が出ている場合は内容を確認し、組織の運用に従って対応してください。

| 状況 | 確認点 |
| --- | --- |
| Outlookを検出できない | クラシック版がインストール・起動されているか。同じWindowsユーザーで起動しているか |
| 「新しいOutlook」だけを使っている | 標準の連携対象外。クラシック版を利用できるか確認 |
| 同期が応答待ち・失敗になる | Outlookのサインイン、プロファイル選択、読み取り確認の画面が開いていないか |
| 会議が0件 | Windowsの日付・タイムゾーン、Outlookの既定予定表、当日のTeams会議があるか |
| 特定の会議だけ出ない | 別の予定表・別アカウントか、取消済みか、有効なTeams参加情報があるか |
| 変更が反映されない | Outlookが最新情報を受信しているか。アプリの同期エラーや最終同期時刻も確認 |
| 組織の設定で拒否される | 管理者へ確認。アプリから制限を解除する機能はない |

予定を取得できなくても、録音デバイスと録音の同意を設定すれば手動録音は利用できます。

## TeamsのCDPを使わない理由

TeamsはWebView2を利用するため、デバッグ接続のCDP経由で画面を読む構成も技術的には可能です。しかし、Microsoftの手順ではデバッグ引数の設定とホストアプリの再起動が必要です。接続後も予定取得がTeamsの画面構造や表示状態に依存します。このアプリでは、既存のクラシック版Outlookから構造化された予定を取得する方式を標準にしています。[WebView2の接続手順](https://learn.microsoft.com/en-us/microsoft-edge/web-platform/devtools-mcp-server#auto-connect-to-a-webview2-instance)、[Outlookの繰り返し予定取得](https://learn.microsoft.com/en-us/office/vba/api/outlook.items.includerecurrences)

Teamsのデバッグポート、起動引数、レジストリは変更しません。Teams画面の読み取りは、録音の参加・退出検知にだけ使います。

## 任意：既存のMicrosoft Graph接続を使う

組織で以前から利用できるGraph接続がある場合は、設定の詳細項目から **Microsoft Graph** を選べます。標準利用のために新しいアプリ登録をする必要はありません。

既存の接続で必要になる情報・設定は次のとおりです。

- 組織から提供されたクライアントIDと、その登録に対応するテナントID。
- パブリッククライアントのデバイスコードフローが許可されていること。
- Microsoft Graphの委任権限 `Calendars.ReadBasic` と、組織が求める同意。

値を保存し、アプリからサインインを開始します。Microsoftの認証ページに表示コードを入力して、利用するアカウントを選んでください。クライアントシークレットは使用しません。組織専用の登録には、その組織のテナントIDを指定します。

Graph版は `calendarView` と `onlineMeeting.joinUrl` を利用します。条件付きアクセスやデバイスコードフローの制限は回避しません。[Microsoftのデスクトップアプリ設定](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration)、[calendarViewの権限](https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview?view=graph-rest-1.0)

Graphの認証キャッシュはWindowsの資格情報ストアに保存されます。標準のOutlookデスクトップ連携ではこの認証キャッシュを使用しません。
