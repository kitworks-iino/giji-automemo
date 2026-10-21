# Teamsデスクトップ連携（プレビュー）

v0.4は、このPCのTeamsデスクトップが読み込んだカレンダー情報を読み取ります。標準方式はWindows UI Automationによる読み取りです。Outlookのインストール・起動、Entraアプリ登録、クライアントID、Microsoft用APIキーは不要です。通常は管理者権限、Teamsの再起動、デバッグ接続の設定も必要ありません。既存のTeamsへのサインインはTeams側で行います。

アプリの連携・録音・ローカル文字起こし・話者分離に追加のサービス利用料はありません。議事録用には手持ちのLLM APIキーを設定し、そのAPIの利用料は契約先の料金に従います。TeamsとPCは既存の利用環境を使います。

**この連携はTeamsが公開する予定取得APIではなく、画面情報に依存するプレビューです。** Windows実機のTeamsアカウントでの取得は開発環境では未検証です。読み取れない状態を「会議なし」と決めつけず、取得状態と確認できた日時を参照してください。予定取得に失敗しても、設定済みの手動録音は利用できます。

**トレイに入れたまま常に全予定を取得する要件は、現状では満たせていません。** 最小化・非表示でも読み取りを試しますが、Teamsがカレンダーの画面情報を破棄・休止した場合は取得できません。初めに利用者が今日の日表示を開く必要があり、予定の読取り処理はTeamsを復元・前面化・操作しません。

## 標準の読み取りを使う

1. Windows版Teamsにサインインし、Teams自身のカレンダーを開きます。**今日** の **日** 表示を選び、当日の予定が見える状態にしてください。
2. Giji AutoMemoの設定で取得元を **Teamsデスクトップ**、読み取り方式を **Teamsが保持する予定表を読み取る**（UI Automation）にして保存します。新規利用では標準設定です。v0.2で保存したOutlookなどの取得元は更新後も保持するため、Teamsを使う場合は自分で変更してください。
3. 予定を読み取り、件名・日時をTeamsの表示と比較します。確認後はTeamsを最小化して利用できます。以後はアプリ起動中、標準60秒ごとに、Teamsが保持する情報の読み取りを試します。トレイ格納後も読めるかはTeams側の状態によります。
4. 録音デバイスとLLMのAPIキー・送信先・モデルを設定し、録音・外部送信への同意と自動録音を確認します。予定を同期しただけでは自動録音を有効にしません。

アプリはTeamsのカレンダー・今日・日表示・予定カード・参加ボタンを自動クリックしません。Teamsの画面切替やスクロールは利用者が行います。アプリやTeamsを再起動した後も、標準方式には接続準備のやり直しはありません。Teamsがカレンダー情報を失った場合や日付が変わった場合は、利用者が当日のカレンダーを開き直す必要があります。

標準方式はレジストリを書き換えず、デバッグポートも開きません。企業端末の実行制限やアクセシビリティ情報の取得制限で読めない場合は、その制限を回避せず、表示された理由を確認してください。

## 最小化・非表示と取得状態

UI Automationの画面要素には、最小化されたために画面外と判定されるものがあります。v0.4は、Windowsのウィンドウ状態から最小化・非表示を確認した場合に限り、その状態だけでは要素を除外しません。同じユーザーのTeamsが所有するウィンドウに限定し、選択中のカレンダーと今日の日付を確認します。別画面の非表示キャッシュや明示的に隠された要素を予定として採用しません。

Teamsを別ウィンドウで覆うこと、最小化すること、×でトレイへ閉じることは異なります。トレイ格納時にカレンダーが残るか、データが更新されるかはTeams側の実装・設定に依存します。読み取れない場合は取得エラーを表示して保存済み予定を残し、Teamsを勝手に開き直しません。

**最終読取り時刻は、画面情報を読めた時刻です。Microsoftのサーバーから最新情報を取得した証明ではありません。** 部分取得には以前の保存済み予定も混在します。取得失敗後に予定が残っていることを、現在の取得成功や予定変更の反映と混同しないでください。

## 読み取れる範囲

- 対象は、接続したTeamsのカレンダー画面から日時を確認できる当日の予定です。日本語・英語の表示を対象とします。予定の時刻だけでなく、カレンダーに今日の日付が表示されていることも確認します。
- 読み込み済みの画面情報と、未生成の仮想化された予定は異なります。後者や別アカウント・別カレンダーの予定まで取得したとは保証しません。今日の予定を漏れなく取得したことは、Teams側の一覧と照合してください。
- **標準方式はWindowsのタイムゾーンを使います。** Teams側に別のタイムゾーンを設定している場合は、日時がずれる可能性があります。TeamsとWindowsの設定を合わせ、表示時刻を比較してください。
- 予定名と時間だけが読み取れ、Teamsの参加URLが画面情報にない場合があります。URLや参加者名を推測して補いません。その場合はアプリで **録音対象に選択** してからTeams側で参加できます。選択だけでは会議への参加や録音は始まりません。
- カレンダーには対面の予定なども含まれます。参加URLが見えない予定を、Teams会議だと画面情報だけで確定できません。標準方式では通常、参加URLを取得できないため、予定の件名を録音に使いたい場合は自分で録音対象を選択してください。未選択でも自動録音は利用でき、その場合は予定と対応付けない件名で保存します。
- 部分取得では、読み取れた予定を当日の保存済み一覧へ追加・更新します。画面に見えないだけで取消とは判定しないため、変更前・取消済みの予定が残る場合があります。変更・取消の完全な反映は保証しません。取得エラーや保存済み表示は、最新情報の確認完了ではありません。
- 読み取った予定を録音へ自動で対応付けるときは、直近の取得で確認でき、有効なTeams参加URLもある予定だけを候補にします。URLのない予定や以前の保存済み予定を使いたい場合は、その内容をTeamsと照合してから自分で録音対象に選択してください。
- 日付や開始・終了時刻を確定できない、対象画面を見つけられない場合は、件数や日時を推測しません。取得失敗は録音停止の条件に使いません。

Teamsの中に `outlook.office.com/hosted/calendar/` の表示が含まれる場合があります。これはTeams内のカレンダー表示であり、PCのOutlookアプリを起動したり、Outlook COMへ自動で切り替えたりするものではありません。

Giji AutoMemoはTeamsのパスワード、Cookie、アクセストークン、認証キャッシュを抽出しません。標準方式・後述の詳細方式とも、Graphや非公開の予定APIへ要求を送りません。予定が読めない場合も、別の取得元へ自動で切り替えません。

## 任意の詳細方式：WebView2 / CDP

標準のUI Automationを使わず、詳細設定で **詳細接続（CDP）** を明示的に選ぶ場合だけ、以下の準備が必要です。この方式も画面情報を読むプレビューであり、取得の成功や網羅性を保証しません。Teamsが所有するカレンダーの別ウィンドウや、画面の表示範囲外でも読み込み済みの要素を対象にできますが、非表示の別画面・非選択のキャッシュは対象外です。復元・前面化・画面移動・クリックは行いません。

MicrosoftはWebView2のブラウザーフラグについて、変更・削除され得るため本番アプリで使わないよう案内しています。`remote-debugging-port` を利用するCDP方式を、恒久的に安定したTeams予定連携として推奨するものではありません。WebView2が休止した場合にCDP接続だけで必ず復帰する保証もなく、トレイでの常時取得を保証できる代替手段ではありません。

WebView2のデバッグ接続には、予定表の読み取りを超える広い操作能力があります。PC内だけの接続でも、同じWindowsユーザーで動く他のプログラムからTeamsの内容を読み取ったり操作したりできる可能性があります。必要な間だけ使用してください。

1. 会議・通話・録音が終わってから、読み取り方式をCDPへ変更して保存します。
2. 説明を読み、**Teams連携を準備** を押します。このWindowsユーザーのTeamsだけに一時的なWebView2デバッグ起動設定を追加します。
3. 準備から **10分以内** にTeamsを通知領域のメニューから完全に終了し、普段の方法で起動し直します。ウィンドウの×だけではバックグラウンドプロセスが残る場合があります。
4. 接続状態を確認し、Teamsのカレンダーを今日の日表示にして同期します。

通常は管理者権限を必要としません。ただし、会社で管理された設定や既存の他のデバッグ設定は上書きせず、接続不可として表示します。組織の制限を解除・回避して利用しないでください。

一時設定は、接続確認、準備から10分の期限、連携解除、アプリ終了の際に元へ戻します。独立した後片付け用プロセスも期限とアプリ終了を監視します。アプリはTeamsを強制終了・自動再起動しません。

**CDPの準備は初回だけではありません。** Giji AutoMemoの再起動、Teamsの再起動による接続切れ、連携解除後の再利用では、再び準備し、Teamsを完全終了・再起動します。接続したまま利用する間は、予定同期のたびに準備し直す必要はありません。

### CDPの利用を終了する

アプリで連携を解除するか標準方式へ切り替えると、CDPの読み取りを止めます。**設定を元へ戻すだけでは、動作中のTeamsが開いたデバッグ接続は閉じません。** 会議・通話が終わってからTeamsを完全に終了し、通常起動してください。これで一時設定のないTeamsへ戻ります。

## うまくいかないとき

| 状況 | 確認点 |
| --- | --- |
| Teamsの予定を読めない | Windows版Teamsを同じユーザーで起動し、今日の日表示を開く。サインインとTeams自身の読み込みも確認 |
| 最小化・トレイ格納後に読めない | カレンダー情報が破棄・休止された可能性がある。前面化は自動で行わないため、必要に応じて自分で今日の日表示を開き直す |
| 予定は残るが取得エラーになる | 保存済み予定の表示。最終読取り時刻を確認し、現在の取得成功やサーバー同期済みとは扱わない |
| 予定が不足する | Teamsの表示範囲、スクロール、予定表・アカウント、日付を確認する。完全取得を前提にしない |
| 予定時刻が違う | WindowsとTeamsのタイムゾーンが一致しているか確認 |
| 会議の参加操作がない | 参加URLを読み取れていない。録音対象を選択し、Teamsから参加する |
| CDPで接続待ちのまま | 準備から10分以内にTeamsを完全終了・再起動したか。アプリやTeamsの再起動後は再び準備する |
| 組織の設定で接続できない | 管理者の運用に従う。Outlook・Graphの利用は詳細設定で自分が選ぶ場合のみ |
| Teams更新後に読めなくなった | 画面構造が変わった可能性がある。手動録音を使い、公開可能なエラーとTeamsの版を報告する |

不具合報告にTeamsの画面全体、会議本文、認証情報、画面の読み取り結果やデバッグ接続の応答をそのまま貼り付けないでください。

## 方式の根拠と検証の限界

Microsoftは、Teamsカレンダーのスクリーンリーダーによる操作と、予定名・時刻の読み上げを説明しています。WebView2にもUI Automation Providerがあります。ただし、Teamsが公開する安定した予定取得APIではなく、すべての版で同じ属性・日時表現を返す保証はありません。公開資料やテスト用データは、利用するTeamsの実機検証の代わりにはなりません。

- [Microsoft: Teamsカレンダーのスクリーンリーダー操作](https://support.microsoft.com/en-us/accessibility/teams/basic-tasks-using-a-screen-reader-with-microsoft-teams-calendar)
- [Microsoft: Teamsの予定名・時刻の読み上げ](https://support.microsoft.com/en-us/accessibility/teams/quick-start-guide-using-microsoft-teams-with-a-screen-reader)
- [Microsoft: WebView2のUI Automation Provider](https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/winrt/interop/icorewebview2compositioncontrollerinterop)
- [Microsoft: Windows UI Automation](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-uiautomationoverview)
- [Microsoft: IsOffscreenと最小化・遮蔽・非表示の違い](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.automationelementinformation.isoffscreen)
- [Microsoft: UI Automationで取得できる仮想化要素の範囲](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-workingwithvirtualizeditems)
- [Microsoft: WebView2の非表示・休止・再開](https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2_3#trysuspend)
- [Microsoft: WebView2ブラウザーフラグの制限](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/webview-features-flags)
- [Microsoft: WebView2のデバッグ接続](https://learn.microsoft.com/en-us/microsoft-edge/webview2/how-to/debug-visual-studio-code)
- [Microsoft Playwright: WebView2への接続](https://playwright.dev/python/docs/webview2)
- [Microsoft: 新しいTeamsの構成とiframe](https://techcommunity.microsoft.com/blog/microsoftteamsblog/microsoft-teams-advantages-of-the-new-architecture/3775704)
- [公開OSSでのhosted calendar表示の報告](https://github.com/IsmaelMartinez/teams-for-linux/issues/2059)
- [Chrome DevTools Protocolの公式スキーマ](https://github.com/ChromeDevTools/devtools-protocol)
