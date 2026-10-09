# OEP 全体のテスト方針

状態: 2026-10-09 の方針と改修計画。OEP、利用プロジェクト、計測・pytest 統合の保証対象と責任を定義する。現在のテストや CI をこの方針へ合わせて改修する。ここに挙げた契約がすべて実装済みという意味ではない。

この文書は OEP プロジェクト共通のテスト方針であり、このリポジトリにある Python テストだけの説明ではない。文書の保守窓口は oep-client-python とし、保証対象の変更は関係する実装の所有者がレビューする。通信の規範・番号・共有ベクタの正本は oep-spec に置き、テスト基盤の設定形式を protocol の適合条件に追加しない。

実際の個体・配線・接続・給電・設備の運用記録は、各設備管理者が公開コードとは別に管理する。公開テストは自分のリポジトリと明示された設定で成立し、他の設備台帳や生成器を必要としない。設備固有の ID、path、合否基準を共有文書へ埋め込まない。

利用側の入口は [OEP を使うプロジェクトのテストガイド](testing-consumers.ja.md)。基盤の設計は [構成と配線の確定](hardware-configuration.ja.md)と[実機設定形式](hardware-schema.ja.md)。現行の Python 実機ハーネスの手順は [tests/hw](../tests/hw/README.ja.md)に分ける。

## 1. 保証する単位

試験単位は「契約 × 実装 × 条件 × 観測方法」である。契約には ID、保証対象、刺激、期待結果と許容差、必要な役割・接続・機能、対応範囲、終了状態を持たせる。契約 ID は物理個体、probe model、固定 channel、実験日を含めない。個体の入れ替えは実行条件の変更として扱う。

コンパイル、仮想環境での成功、DUT 内の自己申告、外部測定、独立実装との相互運用は別の保証である。loopback は自分の TX/RX が同じ誤りを持つ場合を検出できず、同一時計での時間測定は時計の誤差を検出できない。期待値は試験側に置き、設備設定によって合否基準を変えない。個体ごとの校正値は出所・測定条件・有効範囲を記録する。

正常、境界、不正入力、競合、切断・再起動、失敗後の回復を契約に含める。すべての試験で無理に全組合せを作るのではなく、その契約の実装差分に根拠のある軸を選ぶ。

## 2. テスト群と所有者

| テスト群 | 本来保証すること | 主担当と確認方法 |
|---|---|---|
| 仕様の整合性 | 規範、番号、生成物、共有ベクタの整合 | oep-spec の既存検査。実装や設備のテストは置かない |
| 仮想プローブの仕様適合 | 状態遷移、拒否、再送、session、資源寿命、通知が SPEC に沿う | oep-client-python。仕様ベクタと独立した入力・期待値で検査 |
| OEP host | framing、TLV、revision、発見、pipeline、timeout、再送・回復、未知値 | 各 client と ch32rv の OEP 実装。単体＋仮想＋独立実機 |
| broker | client ごとの資源と所有権、共有、切断回復、複数 target の分離 | ch32rv。仮想 fault injection と実機の複数 client |
| プローブの protocol 実装 | list/describe、宣言と実装の一致、session、拒否、資源競合 | oep-probe-arduino。C++ 単体＋独立 host による実機検査 |
| 実際の OEP transport | serial/bulk/HID/TCP の分割・結合、境界、切断、再列挙、同じ個体の認識 | probe 側と各 host 側。probe の USB と DUT の USB は別試験 |
| debug wire と DM/ADI | scan、attach、halt/resume/step/reset、read/write、上限、線切れ、状態保存 | oep-probe-arduino。既知 target と独立した観測・host |
| target 固有の flash/debug | RAM loader、erase/program/readback、境界、部分更新、中断、GDB、console 共存 | ch32rv。仮想＋既知診断 image＋実機。Python 独自経路は Python が担当 |
| プローブの fixture | GPIO、UART、I2C/SPI 相手役、駆動・解放、資源共有 | oep-probe-arduino。既知信号または独立 peer と照合 |
| capture と時計 | rate/layout、trigger、pretrigger、ring、drop、通知、logic/analog/group、時刻不確かさ | probe と各 client。既知信号、保存データ、独立基準で層別に検証 |
| firmware の配布と更新 | profile、identity、manifest/hash、更新対象、更新後の起動・設定・復帰 | oep-probe-arduino。client 側 updater の通信・UI はその client |
| Arduino の定義・ビルド | part/package、pin/route、board/options、startup/link、依存、配布物 | ArduinoCore-CH32RV。生成整合、host、全採用対象 build、隔離 install |
| Arduino の runtime/API | GPIO/interrupt、時間、Print/Stream、UART、Wire、SPI、ADC、PWM、DAC、timer | ArduinoCore-CH32RV。自己検査＋外部刺激・観測＋独立 peer |
| Arduino と tool の接続 | discovery、upload、reset、monitor、HID bootloader、再列挙 | ArduinoCore の最小 E2E。ch32rv は内部処理のより深い検証を持つ |
| DUT の USB | host/device の API、class、transfer、復旧、他周辺との共存 | ArduinoCore-CH32RV。役割別の DUT と peer、独立実装との相互運用 |
| USB PD | CC 交渉、source/sink、fixed/PPS、fault、実 VBUS | ArduinoCore の採用範囲。PD source/sink と独立電圧観測 |
| 計測データと解析 | 取得元、時刻、欠損、file format、decoder、照合と未知・未検査 | wireskein。合成データ、実記録、仮想 OEP、実機取得の別試験 |
| browser の表示 | file format の読取り、logic/analog、添付、時間・値の表示 | wireskein-web。既知 fixture と browser の確認 |
| pytest 統合 | primary/peer の lifecycle、port/profile、終了処理、report、失敗を隠さない | pytest-embedded-arduino-cli と pytest-embedded-wireskein。それぞれの機能を所有側で検証 |

共有ベクタへの適合だけでは全 protocol の適合を主張しない。Python client と同じ Python 仮想プローブの組だけで独立性を主張しない。多言語実装、規範からの期待値、実機との照合を組み合わせる。

## 3. 必須の試験契約

下表の ID は契約分類の案であり、現在のテスト名ではない。個別ケースの値・回数・許容差は、規範の値または対象実装の宣言から定義する。再現試験の回数は契約の失敗モードと実行時間を踏まえて別途決める。

| 分類 | 正常系に加えて必要な確認 | 主な実行軸 |
|---|---|---|
| HOST-FRAME | 分割/結合、最大長の前後、CRC、不正 role/TLV、遅延・欠落・重複 | transport、host 実装、frame/window 境界 |
| HOST-RECOVER | 同じ要求の再送、boot/session 無効化、開き直し、誤った再実行をしない | 切断箇所、再起動、状態変更の有無 |
| BROKER-OWNERSHIP | monitor/flash/GDB の共存、終了した client の資源解放、target 間の混線防止 | client 数、target 数、同じ/別 probe |
| PROBE-DECLARE | revision/ops/limits と挙動の一致、未知機能の拒否、個体識別 | model/profile、transport、実装 SPEC |
| PROBE-SESSION | end/lease/force、pins/streams/plans の解放、競合拒否 | 資源数、複数経路、途中失敗 |
| PROBE-CONFIG | get/set/save/erase、再起動、slot/bind、壊れた保存内容、設定復元 | storage、設定項目、適用順 |
| DEBUG-WIRE | 候補探索、曖昧性、既存接続維持、attach/reset、wire lost、時間上限 | RVSWD/SWIO/SWD、target 系統、clock |
| DEBUG-MULTI | 同じ probe 上の A/B の状態・stream・reset の分離、上限時の拒否 | connection 上限、wire/slot 数、実際の同時性 |
| TOOL-FLASH | 空/非整列/境界/最大 image、部分更新、readback、誤個体拒否、途中失敗 | chip/erase 粒度/ABI、probe backend、transport |
| TOOL-DEBUG | register/RAM、step/breakpoint、reset/run、console を壊さない、detach 後の走行 | chip/DM、HW/flash breakpoint、monitor 共存 |
| FIXTURE-IO | GPIO の方向/idle、UART baud/形式、I2C NACK/stretch/回復、SPI mode/bit order/CS | channel 資源、速度、peer 実装、buffer 境界 |
| CAPTURE-DATA | 既知信号、trigger/segment、drop 検出、layout、多 rate、保存・再読込み | logic/analog、ch 数、rate、load、取得方法 |
| CAPTURE-TIME | 独立した時間基準、不確かさ、probe 間の同期、boot_id の違い | 同一/複数 probe、clock、trigger 方法 |
| UPDATE-PROBE | 目的 image の照合、更新、再接続、設定確認、対応する利用経路の回帰 | model/profile、flasher、更新前後の版 |
| CORE-RUNTIME | startup、constructor、heap/stack、小 RAM、Print/Stream、reset 後の起動 | ABI、family、clock、package、build options |
| CORE-PERIPHERAL | API 値と外部信号、異常入力、timeout、共有 timer/IRQ、解除と再設定 | instance、route、clock、負荷、実装差分 |
| CORE-PACKAGE | install、FQBN、依存の固定、library compile、recipe と選択した実 core | package/local source、board/options、OS |
| USB-ENUM | descriptor/configuration、control request、無効 request、相手の識別 | DUT host/device、controller、speed、class |
| USB-DATA | 双方向の既知 payload、sequence/length/CRC、packet/buffer 境界、short/ZLP | role、class、transfer、payload/load |
| USB-RECOVER | bus reset、stall/clear、USB 脱着、peer reset、再通信、UART/timer 共存 | reset の対象、給電、role、同時動作 |
| PD-POWER | 交渉と実電圧の一致、fixed/PPS、脱着、reset、fault と終了状態 | 採用 role、source/sink、電圧・電流条件 |
| RUN-LIFECYCLE | 両側 READY、開始同期、timeout、キャンセル、ログ・設定の後始末 | single/peer、制御経路、失敗位置 |
| WIRING-RESOLVE | pin map の識別、複数候補、入れ替え、再確認、古い結果の無効化 | probe/target 数、候補範囲、配線変更 |
| EVIDENCE-VERIFY | 欠損・未検査を成功扱いしない、log/waveform/判定の対応 | recorder、decoder、report、保存形式 |

この一覧は USB class や Arduino API の採用範囲を無制限に増やすものではない。対応すると宣言する機能ごとに、正常・異常・復旧の契約を埋める。非対応機能は対応表で対象外とし、未実装・未検証と区別する。

## 4. 実行層とリリース判断

実機なしでは、純粋ロジック、生成物、protocol、仮想 probe/target、host core、配布物の build/install を検査する。仮想接続の peer lifecycle もここで検証する。通常の収集と実行は、設備設定がなくても成立させる。

実機では、probe 単体、probe と既知 target、Arduino DUT と loopback、DUT と独立 peer、DUT と計測器、複数 client/target の契約を分ける。必要設備はテストの要求から選び、物理個体名をテスト本体へ埋め込まない。

リリース確認の対象は所有側が変更前に定義する。代表実機は silicon、ABI、clock、controller、transport、実装経路の差分で選ぶ。全実機の全値の直積を毎回実行する必要はないが、採用した差分軸の未実行を総テスト数で隠さない。

| 変更対象 | リリース前の責任 |
|---|---|
| probe firmware | probe の単体/build、候補 image の実機検査、更新後の復帰、影響する client/ch32rv/Core 経路の回帰を probe 側が実行・判定する |
| host/client | 仮想と設置済み実機で client 側が確認する。更新 UI/transport の変更では updater 契約を追加し、更新作業として明示実行する |
| ch32rv | protocol/broker、独立診断 image での flash/debug、対応 backend の回帰、Arduino recipe/monitor の影響範囲を確認する |
| ArduinoCore | 定義・build・package、API と外部観測、採用 USB role/class、最小 upload/monitor E2E を確認する |
| 計測・plugin | その実装の単体と利用 workflow を確認する。probe firmware の品質保証を代行しない |

利用プロジェクトの通常試験は設置済み probe で動作を確認し、使用版を記録する。probe firmware の最低版を試験設定に定義しない。扱う protocol/interface revision と必要機能は検証する。WCH-Link の既知不良判定と更新は tool 側の責任である。

候補が失敗したら、その契約を必須とするリリースは未完了になる。「結合試験を共有しているから全リポジトリを同時に停止する」という判断にはしない。原因を特定できない失敗でも、必須契約の成立を確認できない事実は残す。

## 5. 配線探索から試験まで

試験は「配線が最初から決まっている」を前提にしない。初期接続の候補、探索結果、確定した構成、今回の役割割当を区別する。

```text
個体・給電・候補範囲を指定
  → probe の宣言と既存資源を確認
  → debug 線と target 個体を探索
  → 必要な GPIO/UART 等の対応を診断 firmware で確認
  → 接続を確定して構成を保存
  → テストの要求から役割を割り当て、実行計画を表示
  → 個体と接続を再確認
  → 実行・観測・終了処理・証拠保存
```

探索自体が書込みや reset を伴う場合があるため、通常の pytest 収集で自動実行しない。試験中に配線が成立しなくなっても、勝手に別 target を探索して継続しない。探索は範囲を明示した準備処理で、失敗原因の候補も記録する。詳細は [構成管理案](hardware-configuration.ja.md) に定義する。

## 6. 複数ターゲットと USB ペア

物理ボードは同じでも、試験中の DUT、peer、観測器の役割は変わる。USB host/device はその試験の役割であり、CH32X035 や ESP32-S3 という board 名に固定しない。

```text
PC ─ OEP probe ─ debug/control ─ target A（今回の DUT）
               └ debug/control ─ target B（今回の peer）
                                 A ─ USB data ─ B
```

debug/control は RVSWD による書込みと DM console 等、USB data は被試験経路である。USB を試すためだけに peer の UART 配線を必須にしない。RVSWD のみの CH32X035 peer は、この構成の正式な対象にする。

### 6.1 同時接続の実装目標

設定モデルは一つの probe に複数 target、複数 probe、一つの target への複数 backend を表現できるようにする。target 個体と debug 経路を分離する。

一つの probe 上で A/B を同時に制御する構成は、wire の max_connections、console 数、pin/CPU 資源が実際に満たされる必要がある。現在の単一 DebugPort や slot の制約を理由に構成モデルを 1:1 へ戻さず、必要な platform の probe/console/config を改修し、実際の上限を宣言する。小さい platform では上限 1 も許容するが、同時制御を要求する契約には割り当てない。

順番に書込み、接続を切り替えて READY/status を確認する構成も別の能力として表現する。ただし切替の間の log 欠損、DM mailbox の待ち、状態保存に制約がある。常時両側制御・同時観測が必要な契約の代用品にはしない。複数 probe で両側を制御する構成も同じ契約へ割り当てられるようにする。

OEP の session lock は probe 単位である。同じ probe の A/B を、独立した pytest peer fixture が別 session で奪い合う構成にはしない。broker または試験所有側の共有 session が両方を管理し、target ごとの操作と stream を分離する。共有 session の transport 切断と client 離脱の扱いも試験する。

### 6.2 USB の試験系列

| DUT と peer | 保証する範囲 |
|---|---|
| DUT host / 既知 device | host の enumeration/control/class/data/回復 |
| DUT device / 既知 host | device の descriptor/class/data/回復 |
| DUT と同じ stack の安価な CH32X035 peer | 繰返し、資源競合、負荷、回復の回帰。独立相互運用とは別の行 |
| ESP32-S3、PC 等の独立 stack | 対応 role/class/speed における相互運用 |
| USB PD source/sink | CC と電圧の契約。USB data の成功からは推定しない |
| HID bootloader upload | 書込みとアプリ起動の契約。sketch USB API の保証には使わない |

両側の書込み・制御・log は被試験 USB と別経路にする。書込み後に両側へ再問合せ可能な READY を送り、build ID と役割を確認してから、USB を開始する。開始応答と enumeration log の取得順を定め、応答待ちで前の log を読み捨てないようにする。終了時には USB、debug、capture をそれぞれ解放する。

USB peer の VBUS 給電元、switch、GND、逆給電の有無は設備の確定情報である。debug の探索結果だけから USB ケーブルの相手や給電関係を推定しない。FS の成功で HS を保証せず、USB controller/speed/class の採用範囲を独立に定義する。

## 7. 選択と判定

テストが定義するのは役割と必要条件である。設定済みで確定した接続の中から条件に合う割当を生成し、同じ物理資源を共有する組は直列化する。探索候補全体や USB に見える全機器をマトリクスにしない。

設備の物理能力、実装が対応すると宣言する範囲、今回の probe の宣言を区別する。候補 firmware が必須 ops を出し忘れたことを理由に、その契約を対象から消さない。宣言の欠落は適合試験の失敗として記録する。

毎回すべての組を実行する必要はない。接続、target、probe、contract の明示選択と、代表差分を含むリリース用選択を用意する。USB pair は物理的に接続している A/B に限る。役割を反転できる設備では A=host/B=device と B=host/A=device を別の実行として計画する。

| 状況 | 記録・扱い |
|---|---|
| 未対応 silicon/機能 | 対象外。対応宣言から区別する |
| 機能が未実装、試験が未定義 | 未実装／試験未整備。対象外や PASS にしない |
| 任意の設備・配線がない | 理由付き skip／未実行 |
| 明示指定した config/個体/接続がない、曖昧 | 設定・準備エラー。先頭候補へ代替しない |
| 確定構成の再確認で不一致 | 古い構成として停止し、再確定を要求する |
| 候補 firmware の READY、宣言、操作が期待に反する | 失敗を保存する。根拠なく設備不足へ分類しない |
| 実行した契約が期待に反する | FAIL。原因未特定でも事実と証拠を保存する |
| 必須リリース契約の未実行・skip | リリース確認は未完了 |

開発時に任意ケースが skip されてもよいが、実行計画には予定・選択・対象外・設備不足を表示する。リリースの成功は必須契約と差分軸が埋まったことから判断し、pytest の終了コードだけで決めない。

## 8. 証拠と再現性

実行ごとに契約 ID、リポジトリの commit/dirty、SPEC の対応版、build profile、core/library/tool/client/probe/peer firmware、image hash、個体、resolved topology の版・hash、役割割当、transport、刺激、期待値、観測、判定、終了状態を保存する。取れない値は unknown と理由を残す。

log、readback、waveform、capture layout/rate、時刻不確かさ、双方の READY/build ID、失敗時の snapshot を結果と紐付ける。取得時刻と試験時刻の違いを明記し、別 boot の時計をそのまま比較しない。認証情報は保存しない。artifact を再利用して decoder/表示の回帰試験を行う際は、取得側の動作保証と分ける。

操作の失敗と後始末の失敗は両方保存し、後始末の例外で元の原因を消さない。設定を元に戻せなかった、DUT に診断 image が残った、peer の USB が有効なままといった終了状態を明示する。

## 9. 調査から分かった改修対象

| 所有側 | 現在確認できた構造 | あるべき姿への改修 |
|---|---|---|
| Python 配線探索 | pins.py は debug/reset 探索を持つ。複数候補・探索上限超過では停止するよう修正済み | 候補の全列挙・個体照合・曖昧性の明示・確定結果の保存。GPIO/UART の機能配線探索は別工程として追加 |
| Arduino probe | DebugPort は単一 connection。Config の place は wire と console を一つ持つ | platform の必要能力に応じて複数 connection/console/slot を実装し、資源上限と分離を検証 |
| Python 実機試験 | boards.py の個体表と GPIO/UART の既定値、環境変数の個別上書き | 個体と確定構成を外部入力へ移し、契約から対象を選ぶ。model/profile と設備個体を分離 |
| ch32rv | 仮想 OEP、flash/monitor、複数 slot の拒否試験あり。broker が probe を所有 | 同じ probe の複数 target の control/log を分離し、pair 起動と target 間の混線防止を検証 |
| ch32rv の仮想依存 | uv.rs は指定 checkout または兄弟ディレクトリの特定 commit を使用し、不足時に Rust test が return する | 必須の仮想検査が未実行だったことを確認可能にし、隔離した取得・固定・CI 依存を所有側で整える |
| ArduinoCore | 現行 loader は target ごとに port/topology/cases を束ね、port の重複を拒否する | probe/target/接続/役割を分離し、1 probe の複数 target と同じ target の別経路を許す |
| ArduinoCore の USB | TinyUSB source はあるが Arduino device/host API の結線と peer 実行は未実装 | 採用 API/controller/class と contract を先に定義し、実装と role 別の peer 試験を一緒に作る |
| 実機リリース文書 | 移行前は同時リリース判定と設備固有の参照が混在 | 文書は責任を分離済み。変更対象別の必須契約を所有側が実行する gate は今後整備する |
| 設備管理 | 各利用環境で管理 | 汎用形式へ export し、探索候補と確定接続、変更後の再確認を分ける |

既存テストは契約の保証に寄与するかで再利用を決める。単なる実装の写し、固定配線への依存、自己申告だけの無条件 PASS は作り直す。既存の有効なベクタ・fault injection・後始末試験は、役割を整理して残す。

## 10. 実装を進める順序

1. 全体契約と対象範囲をレビューする。公開側で保守する契約、role/requirement、判定と証拠の定義を決める。
2. 個体・接続・候補・確定結果・役割のモデルを決める。1 probe/複数 target、複数 probe、USB pair を合成した仮想設備で、解決・曖昧性・資源競合・変更検出を試験する。
3. 隔離した test workspace、雛形、明示設定入力、plan 表示、排他と結果保存を実装する。通常試験に実機操作を入れない。
4. debug 個体探索と GPIO 等の配線確定を実装する。既知の診断 image と制御経路を用い、配線変更・同型複数個体・probe 交換の回帰を追加する。
5. probe の複数接続、console/config、Python/client/ch32rv の経路選択・broker を改修する。単体・仮想で分離と失敗回復を確認してから実機へ進める。
6. probe 単体 → target flash/debug → Arduino runtime/外部 GPIO/UART → fixture/capture と、依存の少ない契約から実機で通す。
7. USB role 別の API と peer を実装し、両側 READY/start/stop と独立 control を確立する。同 stack の回帰と独立 stack の相互運用を別々に追加する。
8. 採用範囲の API/route/controller の差分、異常系、更新・復旧、リリース gate と CI を埋める。所有側の旧テストと文書を置換する。

各段階で schema、interface、改修対象、検証結果をレビューする。offline 検証・plan・仮想 smoke、共通ロック付き probe preflight、明示 pytest 入口、uv/pytest による probe 転送まで実装した。target の新形式 adapter、配線診断の確定 export、複数 connection/broker、USB peer の整備は継続する。

## 11. 調査した正本と実装

- [OEP 適合](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/conformance.ja.md)、[debug wire と connection](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/interfaces/oep-if-debug.ja.md)、[probe config](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/interfaces/oep-if-probe-config.ja.md)。規範は変更しない。
- [Python の pins 探索](https://github.com/Open-Embedded-Probe/oep-client-python/blob/main/src/oep_client/pins.py)、[実機試験](https://github.com/Open-Embedded-Probe/oep-client-python/blob/main/tests/hw/README.ja.md)、[個体表](https://github.com/Open-Embedded-Probe/oep-client-python/blob/main/tests/hw/boards.py)。
- [probe DebugPort](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/src/OepTarget.h)、[Config](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/src/OepConfig.h)、[実装制約](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/docs/implementation-limits.ja.md)、[C++ 単体試験](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/tests/host/run.sh)、[CI](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/.github/workflows/tests.yml)。
- [ch32rv の OEP と broker](https://github.com/ch32-riscv-ug/ch32rv/blob/main/docs/oep-host.ja.md)、[仮想 OEP](https://github.com/ch32-riscv-ug/ch32rv/blob/main/crates/oep/tests/virtual_bench.rs)、[仮想環境取得](https://github.com/ch32-riscv-ug/ch32rv/blob/main/crates/oep/tests/virtual_bench/uv.rs)、[flash E2E](https://github.com/ch32-riscv-ug/ch32rv/blob/main/cli/tests/oep_flash.rs)、[monitor E2E](https://github.com/ch32-riscv-ug/ch32rv/blob/main/cli/tests/oep_monitor.rs)。
- [ArduinoCore の計画](https://github.com/ch32-riscv-ug/ArduinoCore-CH32RV/blob/main/tests/TEST_PLAN.ja.md)、[実装との照合](https://github.com/ch32-riscv-ug/ArduinoCore-CH32RV/blob/main/docs/test-coverage.ja.md)、[現在の構成 loader](https://github.com/ch32-riscv-ug/ArduinoCore-CH32RV/blob/main/tests/harness/bench.py)、[TinyUSB の現状](https://github.com/ch32-riscv-ug/ArduinoCore-CH32RV/blob/main/libraries/TinyUSB/README.ja.md)。
- [pytest Arduino 基礎](https://github.com/tanakamasayuki/pytest-embedded-arduino-cli/blob/main/TESTING_BASICS.ja.md)、[詳細と peer の原則](https://github.com/tanakamasayuki/pytest-embedded-arduino-cli/blob/main/TESTING_ADVANCED.ja.md)、[peer lifecycle の例](https://github.com/tanakamasayuki/pytest-embedded-arduino-cli/blob/main/examples/12_peer_host_core/README.ja.md)。
- [WireSkein の取得試験](https://github.com/Open-Embedded-Probe/wireskein/blob/main/tests/test_oep_virtual_bench.py)、[capture の照合](https://github.com/Open-Embedded-Probe/wireskein/blob/main/docs/capture-test-guide.ja.md)、[pytest 記録統合](https://github.com/Open-Embedded-Probe/pytest-embedded-wireskein/blob/main/tests/test_plugin.py)、[JS client のリリース確認](https://github.com/Open-Embedded-Probe/oep-client-js/blob/main/docs/release.ja.md)、[web viewer のリリース確認](https://github.com/Open-Embedded-Probe/wireskein-web/blob/main/docs/release.ja.md)。
