# NR定期トリガーアプリ 使用関数リスト（ステップ2 成果物）

全関数一覧（manuals/function-list.md、PRO 90項目・X 129項目）から、このプログラムの動作に必要な関数だけを抜き出したもの。設計書 v1.3 の中核12項目を土台に、フェーズごとに必須度を付けた。主軸は WAVE LOGGER PRO（NR-600／NR-500 とも PRO の見込み）、X は差分のみ記す。

前提（関数ではないが成立条件）：
- 接続は USB、対象は1台。オートメーションから起動したインスタンスだけが操作できる（手動で開いたソフトには接続できない）。
- 収録条件は設定ファイル（OpenFile で読み込む）で固定し、データ保存はソフト側の自動保存に任せる。設定ファイルの Excel 転送設定は OFF が必要。
- イベント通知はないため、状態確認はすべて GetState のポーリングで行う。

## 1. 起動・確立（スケジューラ開始時に1回）

| 関数 | 書式 | 必須度 | 用途・判定 |
|---|---|---|---|
| Visible | `object.Visible`（Boolean、取得／設定） | 必須 | ロガー画面の表示／非表示を設定する。既定は非表示 |
| DisplayAlerts | `object.DisplayAlerts`（Boolean、取得／設定） | 必須 | 警告ダイアログを抑制し、無人運用で止まらないようにする |
| Initialize | `object.Initialize` | 必須 | CreateObject の直後に必ず呼ぶ初期化 |
| OpenFile | `Long = object.OpenFile（lpszFilePath As String）` | 必須 | 収録条件の設定ファイルを読み込む。戻り値 0 以外は確立失敗 |
| SetIdentifier | `Long = object.SetIdentifier（lUnitId As Long）` | 必須 | 起動直後の機器 ID は未設定で、設定しないと収集できない（マニュアル第6章）。引数は型式と物理 ID から計算する（NR-500・USB：0〜3、NR-600・USB：4〜7）。有効な引数でも戻り値が 0 以外なら「他の WAVE LOGGER が使用中」 |
| GetIdentifier | `Long = object.GetIdentifier` | 必須 | 設定した機器 ID の確認（確立条件に含める） |
| GetState | `Long = object.GetState` | 必須 | 確立の完了確認（停止＝1 であること） |
| GetActiveFileType | `Long = object.GetActiveFileType` | 必須 | 確立の最後に、開いた設定ファイルの型を基準値として取り込む。型の定義（PRO §7.1.4.32）：0＝開かれているファイルなし、1＝NR-500 用、2＝NR-600 用、3＝NR-600 多チャンネルリンク用 |
| GetErrorCode | `Long = object.GetErrorCode` | 必須 | 確立の段階では使わない（収集中の異常停止専用のコードで、確立失敗の原因は各操作の戻り値・HRESULT で記録する。詳細設計 第4節）。用途は第2節 |

## 2. 発火（グリッド時刻ごと）

| 関数 | 書式 | 必須度 | 用途・判定 |
|---|---|---|---|
| GetState | `Long = object.GetState` | 必須 | 発火前の事前確認。停止（1）以外（収集中など）ならその回はスキップ |
| GetActiveFileType | `Long = object.GetActiveFileType` | 必須 | 状態取得後に、アクティブなファイルの型が確立時に取り込んだ基準値と一致するか確認する。0（閉じられている）なら `OpenFile` で一度だけ開き直し、基準値に戻ることを確認する。基準値でも 0 でもない場合は開き直さず、その回は見送る |
| Start | `Long = object.Start` | 必須 | 測定開始。戻り値 0＝正常、それ以外はエラーコードとして記録 |
| GetState | `Long = object.GetState` | 必須 | 発火後の観測。収集中（4／5 等）への遷移を確認できれば「開始確認済み」、確認できなければ「送信済み・未確認」 |
| GetErrorCode | `Long = object.GetErrorCode` | 必須 | 観測中に状態がエラー停止（11）になったときのみ取得する（詳細設計 第4節）。Start 自体の失敗は戻り値と HRESULT で記録する |
| Trigger | `object.Trigger` | 条件付き | 収録条件がマニュアルトリガ開始の場合のみ、Start 後に使用する |

## 3. 停止・終了

| 関数 | 書式 | 必須度 | 用途・判定 |
|---|---|---|---|
| Stop | `object.Stop` | 条件付き | 手動停止と異常時の停止処理でのみ使う（通常の収録終了はソフト側設定で自動） |
| Quit | `object.Quit` | 必須 | アプリ終了時にオートメーションのインスタンスを閉じる |

## 4. 補助（ログ・診断）

| 関数 | 書式 | 必須度 | 用途・判定 |
|---|---|---|---|
| GetModuleName | `String = object.GetModuleName` | 任意 | 確立できたソフトの確認とログ記録（疎通確認でも使用した） |
| GetUnitInfo | `Long = object.GetUnitInfo（lUnit As Long）` | 任意 | 接続ユニット構成のログ記録。設計はユニット構成に関知しないため必須ではない。なお X の書式は `GetUnitInfo（lGroup As Long, lUnit As Long）` と引数が2つで PRO と異なる（function-list で確認、2026-11-26）。ステップ1は PRO の1引数で実装 |
| GetStartupState | `Long = object.GetStartupState` | 任意（X のみ） | X 対応時に本体の起動状態確認へ使う候補。PRO には無い |

## 集計

- 必須 11：Visible、DisplayAlerts、Initialize、OpenFile、SetIdentifier、GetIdentifier、Start、GetState、GetActiveFileType、GetErrorCode、Quit
- 条件付き 2：Trigger、Stop
- 任意 3：GetModuleName、GetUnitInfo、GetStartupState（X のみ）
- 合計 16 関数（中核12項目は必須・条件付きの中にすべて含まれる）

## 使わない関数（代表例と理由）

- データ取得系（GetData、GetArrayData、GetDataCount、検索系 Find～ など）：計測データの読み出しはこのアプリの役割ではない
- 保存系（SaveFile、SaveCsvFile、SaveCsvFileEx など）：保存はソフト側の自動保存機能に任せる
- ウィンドウ操作系（ActivateWindow、CreateNewWindow、CopyImage など）：画面操作はしない
- マーク・アラーム系（AddMark、ExtractAlarm、GetAlarm など）：解析用途で対象外
- LAN 系（ConnectLan、SetLanConfig など）：接続は USB 前提のため使わない
- 調整系（Adjust、AutoBalance、GetAdjustStatus）：ゼロ点調整は運用対象外
- CAN マニュアル系（ManualSendDataCan、ManualRecvSetIdCan など）：CAN 送受信はしない
- ファイル管理系（CloseActiveFile、GetActiveFile、ActivateFile など）：開くのは設定ファイル1つだけで、入れ替え運用をしない

## PRO と X の差分（このリストの範囲）

- ProgID が違う（PRO：WaveLoggerPro2.Application／体験版は WaveLoggerPro2Demo、X：WaveLoggerX.Application／体験版は WaveLoggerXDemo）。制御モジュールは ProgID の差し替えで対応する
- GetStartupState と GetSystemErrorMessage は X のみ（GetSystemErrorMessage は X 対応時のエラー詳細取得の候補）
- 上記以外の使用関数は、名前・引数・戻り値とも PRO／X で同型
