# 実装ステップ1

## 検証で判明した事項（2026-11-26、検証：もち）

- **引数なしメンバーは COM プロパティ**。GetState／GetErrorCode／GetModuleName／GetIdentifier／Start は、マニュアルの書式どおり引数なし＝プロパティとして実装されており、pywin32 では属性アクセスで値が返る（メソッド呼び出しにすると `'int' object is not callable` で失敗する。tanaka3 の体験版 4.02 で実測）。対策として logger_link に `_ComObject` プロキシを入れ、`_dispatch` で包むようにした（プロパティは引数なし呼び出しとして提示し、実メソッドと属性書き込みはそのまま通す）。プローブ（tools/probe_com.py）も同じ読み方に修正し、tanaka3 で全項目成功（GetState=1、GetErrorCode=0）を確認した。プロキシの単体テストは tests/test_com_proxy.py。
- **体験版の GetIdentifier は設定値と一致しない**。実測：SetIdentifier(4) → 戻り値 0、GetIdentifier → 8。以後の対応は NR-600 が USB／LAN ともに「8＋物理 ID」を返し（4→8、5→9、7→11、8→8、11→11）、NR-500 の引数 0〜3 は戻り値 1 で失敗した（体験版は NR-600 のみを模擬する模様）。製品版の挙動は未確認のため、設計書 第8節に受入項目を追加した。製品版でも乖離がある場合は、確立条件の照合を「設定値そのもの」から「型式ごとの期待値」へ変更する必要がある（ステップ2の判断事項）。
- 上記のため、tanaka3 での LoggerLink.establish のスモークテストは GetIdentifier 段階で正しく失敗し、後片付けの Quit と close が正常に動作することを確認した。OpenFile 以降は実物の設定ファイルがないため未検証。
- テストは検証側でも再実行し、全62件（うちプロキシの4件は検証で追加）が通ることを確認した。
- **照合方式の変更（2026-11-26、本人の承認）**：確立条件の GetIdentifier 照合は「設定値との一致」から「型式ごとの期待値との一致」に変更した（`Config.expected_identifier`：NR-600 は 8＋物理 ID、NR-500 は物理 ID）。mock も実測どおりの返し方に合わせ、テストは全64件通過。


## 起動と検証

- 詳細設計書第5節の JSON をリポジトリ直下の `config.json` に保存し、`python -m src.app` で起動する。別の設定ファイルは `--config PATH` で指定できる。JSON Lines は設定ファイルと同じ階層の `logs/` に出力する。
- 模擬動作には JSON の最上位に `"mock": {"scenario": "normal"}` を追加する。障害は `delay`（`delay_seconds` は既定0.5秒）、`unconfirmed`、`no_response`、`disconnect` で再現できる。観測段階を確認する場合は `busy`、`start_failure`、`error_stop`、`disconnect_on_start`、`disconnect_after_start` も指定できる。`disconnect` は最初の状態問い合わせで1回切断し、再確立できる。
- Python 3.11 以上を使用する。実行に croniter（検証版 6.0.0）、Windows の実 COM とプローブに pywin32、テストに pytest を使う。テストは `python -m pytest` で実行できる。
- tanaka3 の体験版で疎通を確認するときは `python tools/probe_com.py WaveLoggerPro2Demo.Application` を実行する。プローブは各 API の結果と所要時間を表示する。Tkinter の画面操作、実 COM と実機での発火遅延・継続運転は別途確認が必要。

## 未決・判断メモ

- **GetErrorCode の取得条件**。使用関数リストには確立失敗・Start の非ゼロでも取得するとある。詳細設計書第4節を優先し、アプリでは観測中に `GetState=11` を確認した場合だけ取得する。その他の失敗原因には API の戻り値や COM 例外の HRESULT を使う。疎通確認プローブでは依頼どおり GetErrorCode を呼ぶ。
- **タイミング**。設計書の暫定目標50 msを、グリッド到来後に Start の呼び出しを許容する上限にも採用した。リード時刻から約250 ms間隔で状態を更新し、100 ms前から最終取得を依頼する。グリッドまでに最終取得が完了しなければ見送る。設計で長さが未指定の Start 応答待ちは1秒、終了待ちは5秒、手動送信の状態取得期限は1秒。時計の急変検出は壁時計と単調時計の増分差20 msをしきい値とした。Windows の実測で調整する余地がある。
- **時計の後退**。同一プロセス中はローカルの予定日時で確定済みのスロットを識別する。夏時間で同じ壁時刻が繰り返される場合も1回までとする。再起動時に確定履歴をログから復元する仕様は設計書で未指定。
- **再確立**。サーバー切断では旧参照を STA 上で破棄し、該当回が確定した後に再確立を試みる。GetState 異常でサーバーが存続し、収集・読み出しの有無を判断できない間は旧インスタンスを保持する。安全に Quit できる状態を確認してから作り直す。
- **設定と表示**。`mock` は模擬動作用の任意拡張。収録条件ファイルの有効性はサーバーの OpenFile の戻り値で判断する。今すぐ送信には確立済みの接続が必要。画面には起動後の最大200件の結果を表示する。収集ソフトの版は Windows の COM 登録先の実行ファイルから採取し、取得できない場合は `null` を記録する。
- **WAVE LOGGER X**。使用関数リストは GetUnitInfo の型が同じとするが、手元の公式マニュアルでは X の引数が2つで、PRO は1つ。第8節の未決事項に従い、ステップ1は PRO の `GetUnitInfo(0)` を使用する。X の採用が確定した時点で診断 API を実機で確認する。
