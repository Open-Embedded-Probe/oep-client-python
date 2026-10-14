# SPI targetの論理検査

`SpiChecks(checks, adapter).run()` は選択SPECのfixture §4、plan §2、core §6/8/9を第三段階で検査する。
`Checks(send, registry, unit)` と `SampleSpiAdapter(source)` を明示して渡す。
公開checkerは私的ベンチや隣接リポジトリ、設備設定を探索しない。
前提と標準op宣言を含む42項目。共通宣言17項目とSPI metadata5項目は別に適用する。

- state 0、plan開始前configure、未設定read_rx、role 1〜4の各1つ/別pin、拒否前不変を確認する。
- configureのmode/bit_order幅、LSB featureの有無、未知TLV/raw tag、mode/statusを確認する。
- 四つのmodeと宣言されたbit順で論理CS/SCK edgeを与え、CPOL/CPHAの取り込みedge、MISO、MOSI格納を確認する。
  byte途中で終わる11 bit転送はceil(bits/8)のbyte数で、未到着bitを0にする。
- TXなし/足りないTX/未armのMISOは0。未armのMOSIは捨て、transactions/errorsを数える。
- CSが有効でも0 bitならqueue/累計は変わらず、armは次の転送を待つ。nonzero transferはarmを消費する。
- length超過は実bits数を保ち、dataだけprefixへ切る。queueあふれは新しい転送を捨て、transactionsには数える。
  両方起きてもerrorsは1回だけ増える。FIFO/pendingとread後も残る累計を確認する。
- 任意timestampは存在時だけ幅とCS無効化の時計を確認する。空read_rxはcount 0とpendingを確認し、未定義のbits値を0に固定しない。
- armのlength/count、busy、準備不可時のcompleted failedと副作用なし、準備可能後の即時利用を確認する。
  失敗要求の再送も過去のfailedを返し、新しくarmしない。
- configureでqueue/arm/累計を消す。configure/arm/read_rxの同一要求再送は古い副作用を実行しない。
- MISOはCSで駆動/HiZを切り替え、SCK/MOSI/CSは入力を保つ。
  MISOは計算した送信byteだけでなくlogical pinの駆動状態を観測する。
- role 1〜4の組を満たさないplanはunsupported（tag）、同じ割り当ての重複はmalformed。state 1の同じ割り当ては何も変えず、変更はcause 6。
- configureの初期化失敗はcompleted failedで、state 0とすべて0へ戻し、armはcause 6、新しいconfigureで回復する。
- release/end/expiry/forceはstate 0、mode/order 0、queue/arm/累計なし、pins idleへ戻す。
  CS有効・1 bit取得中のrelease/end/expiry/forceも確認し、不完全な取得とMISO駆動を残さない。
  force後の旧要求は新ownerへ触れない。statusはSID 0、他opはsessionを要求する。

独立`SpiModel`はmax_length 8、queue_depth 3、max_clock 1000000 Hz、cs_setup_ns 0、
channel 0〜3、idle入力の論理fixture。LSBを宣言する/しない2構成を明示する。
`cs`/`clock`/`transfer`/`ready`/`advance`は記録される論理刺激。
clockは取り込みedgeで実際のlogical MISO pinを読む。readyはarmを準備できない場合の刺激であり、
その理由を規範へ追加しない。CS開始/終了、clock edge、pin駆動、queue/state、wire交換をtraceに保存する。

単体86件は正常2構成、共通宣言/metadata、caseごとのwire故障、FIFO/arm/bit順/phase/未取得bit、
二重errors・counter/初期化/駆動/timestampの誤り、再送の二重副作用、CS取得中の解放漏れ、
任意timestamp/setup省略、空readの未使用bits、前提不足と不正刺激を確認する。
`full_conformance`はfalse。物理波形・負荷下のcs_setup_ns保証、255 queue/u32 bits/counter上限、
他pin/channel_group、永続config、並行controller/writer/deferred response、production Hostは未検査。
state 0への有効なarmはplanの有無にかかわらずunavailable・cause 6で状態を変えない。
拒否した要求の再送はconfigure後も拒否を保ち、新しい要求だけがarmできる。
設定済みの準備不可はcompleted failedと区別する。未設定と不正length等が重なる場合の優先順は追加していない。

```sh
uv run pytest -q tests/test_conformance_spi.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_spi.py
```
