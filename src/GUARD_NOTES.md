# 単一起動ガードの実装メモ

- `Local\NRTrigger.SingleInstance` で同じ Windows セッション内の起動を制限する。`CreateMutex` の直後に `GetLastError` を確認する。名前付きミューテックスはハンドルが開いている間存続するため、初期所有権は取得せず、最初の起動だけが `mainloop()` の終了までハンドルを保持する。[CreateMutexW](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createmutexw)
- 判定は引数解析の直後に行う。重複時は `MessageBoxW` で通知し、表示に失敗した場合も Tk と `App` の初期化に進まず終了する。これにより、2つ目のプロセスによるログ書き込みやロガー接続を防ぐ。
- pywin32 は `acquire()` 内で読み込む。インポートできない環境では番兵オブジェクトを返し、開発時の起動を許可する。テストでは読み込み先を差し替え、初回取得、重複、pywin32 が使えない場合、通知の成否にかかわらず初期化を省くことを確認する。
