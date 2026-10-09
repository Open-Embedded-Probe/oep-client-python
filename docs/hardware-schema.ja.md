# 実機設定形式 v1 の提案

状態: v1 の初期実装あり。offline validate/plan と virtual smoke が使用可能。実機の discovery/preflight/runner adapter は未実装。2026-10-09。[全体テスト方針](testing-policy.ja.md)と[構成管理方針](hardware-configuration.ja.md)を具体化する。現行の legacy 実機 runner はこの形式を読まない。最小入口は [動作確認手順](hardware-quickstart.ja.md)。OEP の wire protocol や SPEC を変更する提案ではない。

## 所有と境界

汎用形式と参照 validator の保守窓口は、既存実機ハーネスと discovery を持つ `oep-client-python` とする。offline validator と構造上の planner は実装済みで、実機 runner adapter はこれから改修する。形式の文書、雛形、適合例を公開し、probe firmware の合否は `oep-probe-arduino`、client は各 client、Core の合否は Core が所有する。Python の実行依存を Rust client に強制しない。Arduino の build/upload/peer lifecycle は既存 pytest plugin に任せ、設備の役割解決と共有 control session を接続する adapter を設ける。

この文書と同梱の雛形を汎用設定形式の正本として保守する。実設備の台帳と export は各設備管理者が別に持つ。runner がその台帳の path、checkout、生成器を参照しないこと、この雛形だけをコピーして手書きできることを必須条件とする。

## ファイルの用途

| ファイル | 用途 | 使用する入口 |
|---|---|---|
| `hardware.local.toml` | 個体、電気条件、既知配線、探索してよい範囲 | discovery。正常試験には使用しない |
| `hardware.resolved.local.toml` | 同じノードと、確認済み接続・根拠・世代 | plan、preflight、実機試験 |
| `.env` | 上記ファイルと結果・共有ロックの path | `uv run --env-file .env ...` |

resolved は input を include しない。1回の実行に1つの自己完結した resolved を渡す。用途ごとに複数ファイルを作ってもよいが、実行時の merge、検索、暗黙の default は導入しない。対象を増やすときは同じファイルへノードと接続を追加する。台帳が大きくなった場合、私的な生成元を分割しても export はこの形式を維持する。

雛形は [`.env.example`](../tests/hw/.env.example)、[入力 TOML](../tests/hw/hardware.example.toml)、[確定 TOML](../tests/hw/hardware.resolved.example.toml)。同一 probe、2 target、独立 debug control、target 間 USB と GPIO 観測を例示する。**すべて架空**であり、pad 名は置換用、channel は例示用である。`example = true` のまま実機操作を行う実装は禁止する。現在の firmware が同時接続に対応するという意味でもない。

## v1 のデータ

| キー／配列 | 意味・検証 |
|---|---|
| `schema_version` | 整数 `1`。未知版はエラー。firmware 版とは無関係 |
| `kind` | `input` または `resolved`。試験は resolved のみ受理 |
| `example` | 必須 bool。true は offline 検証と plan のみ |
| `configuration_id` / `generation` | 設備集合の名前と正整数の世代。接続・個体変更で世代更新 |
| `confirmed_at` | resolved の確認日時。timezone 付き RFC3339 文字列 |
| `probes[]` | 一意な ID、identity、host transport。target を内包しない |
| `targets[]` | 一意な ID、chip、board、identity。試験 role・build profile は内包しない |
| `power_domains[]` | 影響先ノード `members`、操作方法、信号電圧、GND の確認、evidence |
| `discovery_requests[]` | input の操作範囲。probe/target/protocol/power、許可・除外 channel、pad、書込み許可 |
| `debug_links[]` | probe→target の control 経路。protocol、channel/pad の対応、power、state、evidence |
| `signal_nets[]` | 複数 endpoint を結ぶ信号。signal 種類、各 node の pad または channel、mode、power、state、evidence |
| `usb_links[]` | target 間の data 接続。host/device endpoint、VBUS 給電元と電圧、GND、state、evidence |
| `evidence[]` | 一意な ID、manual/observation、確認内容。観測には tool/revision と artifact の path |

node の ID は probe/target 全体で重複禁止。各種類の接続 ID と evidence ID も種類内で一意にする。参照切れ、型違い、未知キーはエラーとし、typo による設定の無視を防ぐ。相対 path は TOML の所在基準、環境変数の相対 path は実行時 CWD 基準である。TOML の値に環境変数展開や shell 展開をしない。

identity は probe の `oep-unit-id`、target の `chip-uid` を優先する。`manual-label` は `reason` を必須とし、preflight で人の確認が必要な識別として扱う。serial port、chip family、slot label だけで物理個体の一致を判断しない。観測できない UID を生成しない。

v1 の transport は OEP `serial`、明示指定 `tcp`、実機を使わない `virtual`（既存仮想 profile を指定）、debug は `swio` / `rvswd` / `swd` を扱う。serial は path、tcp は host/port を持つ。channel 対応は `data`、`clock`、任意の `reset` を共通キーとし、protocol ごとの本数を offline validator で検証し、profile/pad 機能と予約 pin は今後の preflight で検証する。WCH-Link は別 backend であり OEP probe を装わない。**WCH-Link、計測器、自動電源 switch の具体キーは今回の v1 雛形の対象外**。全体計画から外すのでなく、それらの実機契約を採用する前に追加例と validator を揃える作業として残す。

signal endpoint は target の pad または probe の非負 channel のいずれかを持つ。mode は `input-only` / `output-only` / `bidirectional` / `open-drain`。共有 bus の同時駆動条件、pull-up、アナログ値等は契約に必要な詳細を追加してからその試験を採用する。`digital` の接続だけで I2C、PWM 精度、電圧精度を保証しない。

input に既知配線を記入する場合も同じリンク構造を使用し、`state = "candidate"` とする。resolved は `state = "confirmed"` の接続だけを持つ。未確定候補や失敗は探索 artifact に全件残し、正常試験用ファイルに混在させない。未知の channel 上限、予約 pin、pad 機能は数値だけで判定せず、profile と probe 宣言の照合を必要とする。

probe の観測 firmware、protocol/interface revision、describe と限界値は evidence の artifact に保存する。これらを最低 firmware 版や試験の採用条件に転用しない。実行時には取り直して結果へ保存する。artifact の実ファイルと SHA-256 を実行結果 manifest に記録し、設定ファイルそのものの SHA-256 も外部 manifest に保存する。設定内に自身の hash を埋め込まない。

## テストの選択と判定

テスト所有側の契約には、ID、採用する実装範囲、役割、必要な接続と能力、build profile の対応、同時性、刺激・期待値・終了状態を持たせる。設備 TOML にテスト合否や build matrix を入れない。

例えば USB 契約は `dut` と `peer`、dut の host 実装、peer の device 実装、USB link、各 target の control、2本の同時 console を要求する。設定中の host/device は物理接続の許可方向であり、API 実装の有無を表さない。双方の profile 適合性は試験所有側が判断する。許可方向を逆にする場合は VBUS と周辺回路も再確認する。

実行選択には contract、probe、target、link の ID による明示 selector を用意する。名前の固定リストをテストコードに持たない。指定した構成内の確認済みグラフで条件を満たす割当を列挙し、同一 target の別名割当や未接続 pair を除く。各割当に role→個体→control/link と必要資源を記録する。

| 状況 | 判定 |
|---|---|
| config 未指定、任意の実機 suite | skip。実機を使わない suite はそのまま実行 |
| 明示 config 不在、不正、input を正常試験へ指定 | 設定エラー |
| 明示 selector が不在・一意選択なのに曖昧 | 設定エラー。先頭を選ばない |
| 任意契約に適した物理接続がない | 理由付き skip |
| gate 必須契約に適した接続がない | 不足を記録し gate 未完了。成功扱いしない |
| 保存された個体・接続が preflight と不一致 | 構成無効として実行停止。途中で探索し直さない |
| 採用 platform で必須 op/connection 数が欠落 | 実装の FAIL。宣言不足によって試験を消さない |
| 未採用 platform の能力不足 | 対象外を明記。未実装と設備不足を区別 |

通常実行は選ばれた割当を安定した順序で一巡する。リリース時の代表 matrix と必須契約は試験所有側の manifest で指定し、plan に全割当と未充足を出す。機器が増えたことで無制限に直積を生成しない。同じ役割組の backend/profile 差分は、manifest で指定した軸だけ展開する。

## 入口と優先順位

CLI 名は各 runner 実装時に確定するが、処理境界は次で固定する。

| 入口 | 入力と副作用 |
|---|---|
| validate / plan | 構文・参照・割当の offline 検査。port を開かない |
| discover | 明示 input と範囲で探索。書込み禁止を既定とし、結果候補を保存 |
| confirm / export | 候補選択と根拠から resolved を新規出力。firmware/slot/power を操作しない |
| preflight | ロックを保持して個体・宣言・必要接続を照合。通常の接続に必要な操作は結果へ記録 |
| test | 同じロック保持区間で preflight→upload→READY→試験→cleanup。設置済み probe を使用 |
| probe update / diagnostic flash / slot apply | 書込み・設定変更を明示する独立入口。通常試験や export が暗黙に呼ばない |

共通の指定は `OEP_HW_INPUT`、`OEP_HW_CONFIG`、`OEP_HW_RESULTS`、`OEP_HW_LOCK`。同じ項目は明示 CLI が環境変数に優先する。`.env` は runner で自動読み込みしない。input/config のどちらかを path 探索して補完することもしない。

OEP の経路は resolved から adapter が解決する。既存 Arduino plugin に直接指定した primary/peer port がある場合、**明示指定を無言で上書きせず、解決した経路と照合して不一致は設定エラー**にする。plugin 内の CLI→profile 環境変数→共通環境変数→sketch の優先順は保持する。一般の serial upload/monitor と OEP debug control を別の経路として扱い、同じ文字列の port に押し込まない。

初期実装は `OEP_HW_LOCK` のホスト共通ロックで実機を直列化する。path は明示必須とし、checkout 内に別ロックを自動生成しない。後に個体単位へ細分化する場合も、probe transport alias、target、電源影響先を含めて一括取得する。DUT/peer が同一 probe を使うときは一つの session と broker を共有する。

## 雛形と生成の運用

手書きは3つの example をコピーし、入力を実設備に置き換え、探索・確認を経て resolved を作る。既知配線を手で入力した場合も identity と接続を確認して resolved へ移す。日時や state を書き換えただけで観測済みとして扱わない。

私的台帳からの生成も同じ validator を通す。出力先を必須にし、通常は新規ファイルへ書いて差分を確認する。既存 `.env` 全体を再生成して秘密値・手編集を消さない。自動生成する4つの path だけを `.env.generated` 等へ提案し、採用時に明示して更新する。生成しても `.env` の自動読込みは始めない。

公開側の実装前に、(1) scope と契約 manifest、(2) schema と正常／異常の適合 fixture、(3) offline loader と planner、(4) shared session adapter、(5) discovery の全候補保存と confirm、(6) 実機 preflight と試験、の順で整備する。WCH-Link・観測器・switch、USB profile と給電制約の追加例を含め、実装に必要な形式をレビューしてから公開側へ反映する。

## 初期実装の保証範囲

`oep-hardware validate` は本雛形のキー・型・参照を検査し、`plan` は PROBE-DECLARE / TOOL-FLASH / USB-DATA の接続に基づく割当を表示する。plan は宣言能力、pad の機能、電気条件、build profile を実測・認定しない。USB pair は既知リンクのみを列挙し、同一 probe 上で重複する debug channel を持つ同時割当を除く。必要 connection 数を表示し、実際の能力不足を暗黙 skip へ変換しない。

`smoke` は明示 virtual transport だけを受理し、COBS/CRC と OEP の confirm、list/describe、identity、session open/end をプロセス内で確認する。`example = true` でも実機を使わない仮想 smoke は可能とする。結果はこの限定した確認範囲の成功であり、PROBE-DECLARE の全適合、実 transport、複数 target、USB 通信を保証しない。

`--config` が環境変数より優先し、`OEP_HW_CONFIG` を読める。validate は config が指定されなければ `OEP_HW_INPUT` も使える。`--out` は新規 JSON のみを作り、既存ファイルを上書きしない。`OEP_HW_RESULTS` の自動採番と `OEP_HW_LOCK` の実機連携は未実装。実機操作を持たないこれらの入口では共通設備ロックを取得しない。
