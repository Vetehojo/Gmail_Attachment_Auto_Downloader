# Gmail Attachment Downloader セットアップ手順

## 対象機能

このソフトウェアは **Gmailの添付ファイルだけ** を自動保存する。

- Gmail本文中のURLは処理しない
- ブラウザ自動操作（Chrome / Selenium / WebDriver）は使わない
- 添付ファイルはアカウントごとに指定した保存先へ保存する
- メールボックスへの書き込みは行わない（`gmail.readonly` のみ）

## 前提

- Windows 10 / 11
- Python 3.14 専用（`Add Python to PATH` を有効化）。3.13 以下・3.15 以上は対応せず、setup.bat が停止する
- Gmail APIを有効化したGoogle Cloudプロジェクト
- 認証方式に応じたJSON
  - OAuth: デスクトップアプリ用OAuthクライアントJSON
  - DWD: Domain-Wide Delegationを有効化したサービスアカウントJSON

## 初回セットアップ

`setup.bat` を実行する。Pythonと依存パッケージを確認・導入したあと、設定GUIが開く。

### OAuthモード

「個人Gmail / 単一Google Workspace（OAuth）」を選び、次を指定する。

- 対象メールアドレス
- 添付ファイルの保存先
- OAuthクライアントJSONファイル

「Googleに接続してテスト」を実行するとブラウザOAuthを行い、成功後は `token.json` を生成する。

### Google Workspace DWDモード

「Google Workspace（Domain-Wide Delegation）」を選び、次を指定する。

- サービスアカウントJSONファイル
- 処理対象アカウント一覧
  - 対象メールアドレス
  - そのメールボックス専用の添付ファイルの保存先

例:

```text
osaka@example.jp  -> C:\MailDL\Osaka
nara@example.jp   -> C:\MailDL\Nara
kobe@example.jp   -> C:\MailDL\Kobe
```

Google Admin Console側ではサービスアカウントのClient IDに対し、次のscopeだけをDomain-Wide Delegationで承認する。

```text
https://www.googleapis.com/auth/gmail.readonly
```

「登録した全アカウントに接続してテスト」は登録済み全アカウントについて `users.getProfile(userId="me")` まで実行する。

認証情報（OAuthクライアントJSON / サービスアカウントJSON / `token.json`）は `%LOCALAPPDATA%\GmailAutoDownloader\` へコピーし、Windows ACLの継承を切って実行ユーザーだけにアクセスを絞る。

## Gmail検索

OAuth / DWDのどちらも、検索条件は `in:inbox` と除外ラベルだけで、`to:<対象メール>` は **付けない**。

- OAuthは認証したアカウント自身のMailboxを見る
- DWDは `.with_subject(account_email)` で対象Mailboxを確定している

どちらもMailboxがすでに一意なので、`to:` を足しても絞り込みにならず取りこぼしが増えるだけになる。このため次の経路で届いたメールも検索対象になる。

- alias宛
- BCC
- Google Group / ML経由
- 他アカウントからのforwarding
- Admin Consoleのルーティング（Also deliver to）で複数Mailboxへ配信

除外ラベルは `excluded_labels` 設定(config.ini / 設定GUI)で指定する。

## 初回取込期間と長期停止

初回設定では以下から選択できる。

- 今日
- 過去3日
- 過去7日
- 過去30日
- 日付指定（YYYY-MM-DD）

`lookback_days` は **初回scanだけ** に使う。durable cursor作成後は、PCが7日以上停止しても `cursor - 5分` から再開するため、長期停止で検索期間を切り捨てない。

DWDではカーソルをアカウント単位に分離する。

```text
mail_cursor_timestamp:osaka@example.jp
mail_cursor_timestamp:nara@example.jp
mail_cursor_timestamp:kobe@example.jp
```

1アカウントのscanに失敗しても、そのアカウントのカーソルだけを進めず、他アカウントのscanは継続する。

## 添付ファイルの重複仕様

job identityは次で決める。

```text
account + Gmail message ID + 添付の識別子
```

添付の識別子は、通常の添付では `attachmentId`、本文に埋め込まれた小さいMIME partでは `partId` + 中身のSHA-256を使う。

したがって、同じメールを5分overlapで再検索しても同じjobは増えない。

一方、送信者が後日まったく同じPDFを別メールで再送した場合は **新しい受領物として再保存する**。

```text
document.pdf
後日の再送 -> document(1).pdf
```

SHA-256による「別メール間の内容重複排除」は行わない。

## crash時の重複防止

添付保存前に、jobごとのcommit journalへ次を記録する。

- 保存予定path
- 添付SHA-256

```text
journal prepared
  ↓
final file保存
  ↓
journal committed
  ↓
SQLite success
```

final file保存後、SQLite success前にPC停止しても、次回起動時にjournalとfinal fileのSHA-256を照合して同じjobをsuccessへ復元する。同じjobで枝番ファイルを増やさない。

## 認証障害

OAuth token失効やDWD認証障害は通常のダウンロード失敗とは分離する。

- jobのattempt数を消費しない
- 15分後へdeferする
- scan cursorは成功していないアカウントについて進めない
- トレイの状態を「要確認」にする

## タスクトレイ

`register_logon_task.bat` はログオン時に `pythonw.exe gmail_app.py` を起動する。VBSラッパーは使用しない。

トレイメニュー:

- 状態を表示
- 今すぐGmailを確認
- 一時停止 / 再開
- 失敗した添付
- 添付を取り直す
- 期間を指定して再確認
- 設定
- 添付ファイルの保存先を開く
- 終了（自動取得を停止）

失敗jobが増えるとトレイアイコンを警告状態に変更し、可能なら通知を出す。最終的にfailedとなった添付は保存先にも `!取得エラー_YYYYMMDD.txt` を追記する。

「失敗した添付」画面では「もう一度取得する」「無視する」「無視を解除」が可能。行を選ぶと、一覧では列幅に収まらないエラー内容・件名・添付ファイル名を下の詳細欄に全文表示する。

「添付を取り直す」画面では、直近90日以内の成功jobを明示的に再取得できる。保存済みファイルを誤削除した場合などに使用する。

## scannerとworker

Gmail scanと添付ダウンロードは別スレッドで動作する。

```text
scanner
  Gmail pagination -> SQLiteへattachment job登録 -> cursor更新

worker
  SQLiteから1件claim -> Gmail attachment取得 -> 保存
```

添付ダウンロードが遅くてもGmail scan自体は停止しない。ファイル保存は直列worker 1本で行う。

## retention

成功jobは無制限に詳細データを保持しない。

- 730日を過ぎたsuccess recordを削除
- 残ったもののうち90日を過ぎたものは `payload_json` / `result_json` をcompact

削除を先に行うので、消える直前のrecordを書き換え直さない。

## 手動スクリプト

| ファイル | 用途 |
| --- | --- |
| `setup.bat` | 依存導入 + 設定GUI |
| `register_logon_task.bat` | ログオン時起動 + watchdogのタスク登録 |
| `start_monitor.bat` | monitorを前面で起動（切り分け用） |
| `stop_monitor.bat` | monitorプロセスツリーを停止 |
| `start_test.bat` | Gmail API接続と直近メール検索だけ確認 |
| `test_latest_attachment_download.bat` | 本番と同じ経路で1サイクル実行 |
| `tests\run_virtual_download_test.bat` | ユニットテスト一式 |
| `reset_last_check.bat` | メールカーソルを戻して再スキャン |

## テスト

GitHub ActionsのWindows / Python 3.14で、runtime moduleのcompile checkと `tests/` 配下の全テスト(`python -m unittest discover -s tests -v`)を実行し、主に次を確認する。

- runtime module compile
- Gmail pagination
- cursorがlookback_daysで切り捨てられないこと
- 初回scanだけlookback_daysを使うこと
- OAuth / DWDとも queryに `to:` を付けないこと
- DWD `with_subject()` + `gmail.readonly`
- DWDのアカウント別cursorと保存先が独立していること
- 1アカウントのscan失敗が他アカウントを止めず、失敗側のcursorも進めないこと
- 認証障害deferでattemptを消費しないこと
- ignored jobをGUI用APIで復帰できること
- final file保存後のcrash recovery
- 同じjobは再保存せず、別メールの同一添付は枝番保存すること
- 本文埋め込み（inline）添付をattachments API呼び出しなしで保存できること
- retention
- `%` を含む設定値でもConfigParser interpolation errorにならないこと

実機ではOAuth/DWD認証、実Gmail添付保存、ログオンタスク、スリープ/復帰後の動作を最終確認する。
