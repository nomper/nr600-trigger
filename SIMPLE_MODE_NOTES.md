# シンプルモード実装記録

## 変更ファイル

- `src/config.py` に `link_mode` を追加した。既定値は `full` で、省略した既存の設定も読み込める。`full` と `simple` を受け付け、既存の `save_config` で保存する。
- `src/logger_link.py` に確立、事前確認、発火結果、スナップショット、終了のシンプルモード分岐を追加した。
- `tests/test_config.py` で既定値、不正値の拒否、保存と読み直しを検証した。`tests/test_simple_mode.py` で模擬サーバーを使った接続処理とログを検証し、`tests/test_scheduler.py` で予定時刻の発火から結果記録までを検証した。

## 設計判断

- シンプルモードは `DispatchEx`、`Initialize`、表示設定、`SetIdentifier`、`OpenFile` の順に処理して確立する。接続済みのサーバーを再利用し、未接続のサーバーが残っていれば `Quit` を試みてから作り直す。確立ログには両モードで `link_mode` を記録し、シンプルモードの `confirmed_identifier` と `file_type_baseline` は `null` になる。
- 事前確認では `Reading(state=1)` を完了済みの Future で返し、スケジューラの既存の `can_start` 判定に渡す。ログとスナップショットには、未取得の状態を `state=null` として記録する。
- `Start` が `0` を返すと、状態観測を省いて「送信済み」（`Start=0`）を返す。`Start` の非ゼロ戻り値、例外、タイムアウトには既存の処理を使う。終了時は `GetState` を省き、直接 `Quit` を試みる。
- COM 操作は引き続き STA ワーカーで行い、ハング検知と再確立のバックオフは既存の経路を共用する。シンプルモードの事前確認とスナップショットは呼び出し元で完結する。

## テスト結果

| タイムゾーン | 全件の結果 |
| --- | --- |
| `TZ=Etc/UTC` | 156 件合格（26.24 秒） |
| `TZ=Asia/Tokyo` | 156 件合格（26.10 秒） |

両方で `python3 -m pytest -q -p no:cacheprovider tests` を実行した。既存 142 件と追加 14 件を含む。実行環境は Python 3.12.3、pytest 9.2.0、croniter 7.0.0。

`GetIdentifier`、`GetActiveFileType`、`GetState` が呼ばれると例外になる模擬サーバーで、シンプルモードの確立、送信、終了を確認した。無印 WAVE LOGGER と NR-500 の実機確認は未実施。

完了
