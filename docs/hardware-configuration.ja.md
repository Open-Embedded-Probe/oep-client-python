# 実機構成と配線を探索して確定する設計

状態: [全体テスト方針](testing-policy.ja.md)から導く構成管理の設計案。2026-10-09（Asia/Tokyo）。ここに記した schema と処理境界の設計、および初期 offline 実装を示す。現行 runner が読み込める設定の説明ではない。

一つの probe に一つの target を束ねた設定をやめ、物理個体、接続、探索候補、確定情報、試験時の役割を分ける。テスト本体は固定 channel や個体名を持たず、論理信号と必要な能力を要求する。設定を読み込んだだけで探索、書込み、firmware 更新が始まる構成にはしない。

## 1. 管理する情報

| 情報 | 内容と正本 |
|---|---|
| probe 個体 | ID、識別方法、host からの transport、firmware 更新経路。交換可能な物理ノード |
| target 個体 | chip/board/package、UID 等の個体識別。probe の slot や port とは独立 |
| 観測器・電源・switch | 使用方法、計測条件、資源 ID、操作によって影響する個体群 |
| 接続 | debug、GPIO/UART、USB data、給電の経路。異なる種類の接続を別々に表現 |
| 探索範囲 | 操作してよい probe channel と target pad、除外、電圧・給電、探索に許す操作 |
| 探索結果 | 候補の全列挙、観測値、個体情報、診断 image、探索 tool/版、日時 |
| 確定構成 | 確認された接続、論理信号と実 pad/channel の対応、根拠と世代 |
| テスト要求 | DUT/peer/observer/control の役割、機能、接続、同時性、刺激と期待値。試験所有側で保守 |
| 実行計画 | 確定構成から今回使う role と経路、資源占有、build/profile、契約の組を解決したもの |

probe 自身の USB transport と target 間の USB data は別の接続である。RVSWD は書込み・debug・DM console の制御経路で、GPIO 観測や USB data の配線があることを意味しない。

同じ target を OEP と WCH-Link の別経路から使う場合は target ノードを一つにして経路を二つ持つ。同じ個体への二重書込みを防ぐため、排他は port 名だけでなく target 個体にも掛ける。

信号線は必ずしも1対1ではない。I2C bus、SPI の共有 clock/data、入力専用の capture 分岐を複数 endpoint の接続として表現する。各線の入力/出力/open-drain、共有可能性、同時使用条件を契約と照合する。

## 2. 探索の開始に必要な情報

適当に配線した後に pad/channel の対応を探索することは許す。ただし給電、GND、信号電圧、USB VBUS の給電元、操作禁止 pin、電源 switch は事前に分かっているものとする。データ線の探索からこれらの電気条件は確定できない。

初期設定は、操作対象 probe の識別、探索可能な channel、target の候補または期待する種類、既知の給電・接続範囲を持つ。target の UID がまだ分からない場合は探索で取得し、確定時に関連づける。取得できない場合は明示的な手動関連づけを記録し、chip family だけで同型の複数個体を一意とみなさない。

probe の describe は機能と許される channel の情報として利用する。候補範囲と宣言の交差を取るが、宣言を電気的な実配線の証明として扱わない。予約 pin、既存 connection、plan、slot、capture、他の操作中の資源を照合する。

## 3. 探索と確定の手順

1. 明示指定された候補設定を読み、型・参照・識別・操作範囲を検証する。対象に関係する probe、target、電源・共有資源のロックを取得する。
2. probe の identity、firmware、protocol/interface revision、channel/ops/limits、現在の資源状態を記録する。
3. debug 候補を分類し、許された範囲の RVSWD/SWIO/SWD を探索する。候補を全列挙し、pins、識別子、試行条件を保存する。複数回答の先頭を暗黙に選ばない。
4. 候補ごとに必要な接続を行い、chip/UID 等を確認する。同型複数個体や UID 不明で関連づけが曖昧なら、明示選択または追加の識別手順を要求する。
5. debug/control が確定した target へ既知の診断 image を書き込む場合は、探索処理とは別に書込みを明示する。image の出所と hash、元の image の扱い、終了状態を記録する。
6. 必要な GPIO/UART 等を論理信号へ対応づける。target pad ごとの識別パターンを、独立した観測側の probe channel で照合する。必要な場合は逆方向の刺激も行い、候補・分岐・短絡・方向を区別する。
7. USB ケーブルや給電など探索できない接続を入力情報と独立確認で確定する。debug が繋がったという理由だけで USB 相手を割り当てない。
8. 曖昧な対応を除き、確定構成を新しい世代として保存する。必要なら probe.config の slot/label を派生設定として生成し、適用を明示してから probe の state と照合する。
9. 変更後の構成と診断 image の状態を表示し、ロックと出力を解放する。探索失敗も artifact として残す。

GPIO の識別パターンは「任意 pad を一斉に駆動」するものにしない。候補に含めた機能・pad の条件に従い、他の出力と衝突しない順序で行う。観測されない信号は未接続・未確定として残し、必須ではない配線まで揃えることを要求しない。

現在の Python pins 探索は debug/reset が中心で、GPIO の target pad と channel の一般対応は確定しない。したがって、debug 探索と機能配線の診断を別の契約・実装工程にする。USB やアナログの物理特性を GPIO 探索の結果から推定しない。

診断 firmware の選択には循環依存を避ける。配線確定の基準は既知の診断 image と独立した観測で作り、検証対象の Arduino API が正しく動くことだけを前提にしない。検証対象 firmware を使って追加確認する場合、その失敗を根拠なく設備不足へ分類しない。

## 4. 確定結果の有効性

確定構成には probe と target の identity、探索 tool、diagnostic image、probe の宣言、接続と signal map、日時、確認方法、configuration generation/hash を持たせる。宣言されたもの、人が入力したもの、観測したものを区別する。

probe 交換、配線変更、target 交換、給電・USB 接続変更では関係する接続を未確定へ戻す。probe firmware/profile が変わった場合は channel/ops/limits と保存設定を再照合し、影響する機能配線を再確認する。すべてを無条件に再探索するのでも、firmware 文字列だけで有効とみなすのでもない。

任意の手作業による配線変更をソフトウェアだけで完全検出することはできない。設備変更後は明示的に世代を更新し、実行前には identity と必要な接続の軽い確認を行う。接続確認は探し直しではなく、確定した相手と対応が今も成立するかの確認である。

確認に失敗したら、その構成での実行を止める。正常試験の途中で別候補を探索して結果をつなげない。動的な再接続・scan 自体を検証する契約だけは、対象集合と期待動作をテスト側が指定する。

## 5. 役割とマトリクスの解決

選択単位は個体単独ではなく、契約を実行できる役割の割当である。

| 契約の例 | 必要な割当 |
|---|---|
| probe 保存設定 | probe、必要なら設定試験用 channel。target 不要 |
| ch32rv flash | target、debug 経路、probe/backend、readback と実行確認の経路 |
| Arduino PWM | DUT、PWM 可能 pad、capture/計測器、確定した観測配線 |
| USB host | DUT host、peer device、両側の独立 control、物理 USB link、VBUS |
| USB device | DUT device、peer host、両側の独立 control、物理 USB link、VBUS |
| 複数 target の分離 | target A/B、同じ probe、必要な connection と console の同時能力 |

テストは `dut`、`peer` 等の論理役割と要求を定義し、設備側の任意の名前を固定しない。TOML にある確定した個体・接続の集合から条件を満たす組を列挙する。複数 probe/target の直積や、未接続の USB pair は生成しない。

probe 一台で複数 target を control する割当と、複数 probe で両側を control する割当を同じ契約に使えるようにする。ただし試験が要求する同時性・観測能力を満たす必要がある。各試験の host/device は build profile を含めて実行時に決める。

開発時は個体・接続・契約を絞れるようにし、リリース時は対象差分を選ぶ。`plan` に相当する入口は、操作せずに割当、未確定、能力不足、未実装、資源共有、実行順を表示する。個体の実在や能力を読み取る preflight は別に明示する。

対応すると宣言する実装の契約は、候補 firmware が ops を出し忘れたからといってマトリクスから消さない。設備の物理能力、実装の採用範囲、今回観測した宣言を分け、必須宣言の欠落はその実装の失敗として検査する。

## 6. 排他と実行順

排他対象は probe、target、共有電源、USB switch、計測器を含む。同じ target への別 backend、同じ probe の別 transport、同じハブ電源で影響を受ける機器群は別名でも競合する。

初期実装では設定で渡されたホスト共通ロックを使用し、全実機操作を直列にしてよい。将来の並列化は資源集合から判断し、複数ロックの取得順を固定して deadlock を防ぐ。ロックの意味は公開側の汎用契約にし、私的リポジトリの位置から計算しない。

同じ probe 上の target A/B は probe の OEP session を共有する。pytest の DUT/peer ごとの lifecycle と、probe の transport/session の所有を別層にし、同じ probe を二度排他的に開かない。書込みは必要なら直列に行い、両側が READY になってから通信を開始する。片側終了で他方の stream/session を破棄しない。

電源操作はその port の DUT だけに効くとは限らない。操作の影響先を構成に含め、契約が意図する reset と実際の影響を照合する。USB data の切断と、probe の transport 切断と、DUT の電源断を区別する。

## 7. ファイルと環境変数

実機試験を所有する側は、汎用の入力 schema、読み込みと検証、役割解決、テスト、雛形を持つ。設備管理者が持つ台帳や生成器はこの schema に従って設定を渡すだけで、公開テストから import、subprocess 起動、checkout 自動探索をされる依存にはしない。別の人が手書きしても同じ構成を作れるようにする。

```text
tests/hw/
  .env.example                   公開する変数名と説明
  hardware.example.toml          公開する設備入力の雛形
  hardware.resolved.example.toml 公開する確定構成の雛形
  .env                           ローカルの実行指定
  hardware.local.toml             初期個体・探索範囲・既知配線
  hardware.resolved.local.toml    確認された接続。明示的に生成または記入
```

`.env` と `*.local.toml` は Git 管理外にする。実際の file 名や出力先は変更可能だが、雛形とローカル設定の区別は維持する。確定構成は self-contained にして初期入力ファイルへの include を要求しない。手で全配線を記入した環境も、検証して同じ確定形式へ出力する。

環境変数の案:

```dotenv
# tests/hw/ から実行する想定。offline CLI の設定と、今後の実機 adapter の設定。
OEP_HW_CONFIG=./hardware.resolved.local.toml
OEP_HW_RESULTS=./.pytest-results
```

`.env` は `uv run --env-file .env ...` によって明示的に渡す。自動読込みを実装しない。config はこの変数または明示 CLI で指定したファイルだけを読み、兄弟ディレクトリやホームから探索しない。CLI に同じ項目がある場合は CLI を優先する。既存 Arduino plugin の primary/peer port/profile の優先順位は維持し、解決済み経路を一箇所から渡す。

相対 `OEP_HW_CONFIG` は実行時の作業ディレクトリ基準とし、このリポジトリの雛形は `tests/hw/` から実行する想定とする。TOML 内の相対パスはその TOML の所在基準とする。log に解決した絶対パスを残す。CI では `.env` を置かず、同じ値を環境変数や CLI で渡せるようにする。

大量の per-device channel 上書きを `.env` に列挙しない。個体と配線は TOML で持ち、build 条件・期待値は試験所有側で持つ。秘密値が必要なケースでは環境から別途渡し、生成 TOML と artifact に含めない。

初期はホストまたは用途ごとに1つの self-contained TOML を選ぶ。複数ファイルの include、暗黙の探索、配列の merge は導入しない。規模が増えたときの分割は私的台帳側で行え、公開側へ渡す export は一つに保てる。

## 8. 生成と更新

通常は `.env.example` と TOML 雛形をコピーして記入する。設備台帳を持つ環境では、初期設定を生成し、探索・確定を経て runner 用の確定設定と `.env` を出力する。schema version、生成元 revision、確定 generation/hash を追跡する。

既存 `.env` を無条件に上書きしない。生成は出力先を明示し、差分確認と置換を別段階にする。ユーザーが指定する実行対象と、機械が確定した signal map の所有を分ける。派生設定を更新しても手編集した秘密値や実行オプションを消さない。

再生成はファイルを作るだけで、firmware 更新、slot 保存、電源操作を行わない。それらは明示的な別の処理として扱う。通常の client/Core テストは設置済み probe を使う。probe 更新後は影響する確定情報を再確認し、新しい export を作る。

## 9. schema を確定する前の検討項目

| 項目 | 推奨する方向 |
|---|---|
| 契約の宣言形式 | ID、role、requirement、観測、採用範囲を所有側で定義する。既存 plugin の fixture/lifecycle を利用する |
| 確定情報の形式 | input と resolved は同じノード・接続概念を使い、根拠・世代を resolved に追加する |
| 制御の共有 | 同一 probe の transport/session を共有し、target ごとの stream を分離する。既存 broker を活用できる範囲を検証する |
| discovery の診断 image | target family/board の制約を持つ既知 image。実装の検証用 sketch と役割を分ける |
| target の個体識別 | UID を優先し、取得不能時の明示対応を定義する。family や slot 名だけで物理個体としない |
| 実機対象の選択 | 明示した構成の範囲内で role を解決し、実行前に plan を確認できるようにする |
| 共通 schema の保守 | oep-client-python を保守窓口として schema と雛形を同期する。設備固有の台帳に正本を置かない |
| platform ごとの複数接続 | 採用 platform と必要 connection/console 数を決め、実装差分と試験契約へ落とす |

具体的なキー、選択と判定、処理の入口を [実機設定形式 v1 の提案](hardware-schema.ja.md)へまとめた。`.env.example` と input/resolved の TOML 雛形も同時に管理する。これはレビュー用の案で、offline CLI と loader は実装済みで、probe preflight・共通ロック・結果保存と明示 pytest 入口も実装済みである。target/peer runner adapter と配線診断の確定・変更検出は今後追加する。
