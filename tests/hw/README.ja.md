# 実機の結合試験（`tests/hw`）

[English](README.md)

現在の Python 実機ハーネスの手順です。共通の責任と保証対象は [全体テスト方針](../../docs/testing-policy.ja.md)、OEP を利用する任意のプロジェクト向けの進め方は [利用者ガイド](../../docs/testing-consumers.ja.md)を参照してください。プローブ更新の検証は firmware 所有側、クライアントの動作は client 側が判定します。両方のリリースを一律に連動させません。

通常の `uv run pytest` は仮想ベンチ等のボード不要検査を行います。このディレクトリの実機検査は `OEP_HW_BOARDS` を指定しない限り skip します。**新しい設備 TOML は `oep-hardware preflight` と `pytest tests/equipment` で probe 単体確認に使用できます。[手順](../../docs/hardware-quickstart.ja.md)を参照してください。このディレクトリの legacy target suite への adapter は未実装です。** `.env.example`、`hardware.example.toml`、`hardware.resolved.example.toml` は [設定形式案](../../docs/hardware-schema.ja.md)の雛形で、offline 検査と plan には使えますが、この実機 suite が読み込むものではありません。

現行入口は repository root で実行します。`PROBE_ID` は現在の `boards.py` が受理する明示 ID に置き換えます。仮想例以外のコマンドは実機を操作します。

```sh
# 設置済み firmware を使う。firmware 書込みを省く指定で、read-only 試験ではない。
OEP_HW_BOARDS="$PROBE_ID" OEP_HW_NOFLASH=1 uv run pytest tests/hw -m hw

# probe 所有側が候補 checkout と更新対象を明示して検証する場合。
OEP_HW_BOARDS="$PROBE_ID" OEP_PROBE_DIR=/path/to/oep-probe-arduino uv run pytest tests/hw -m hw

# probe 所有側が明示した配布 image の更新を検証する場合。
OEP_HW_BOARDS="$PROBE_ID" OEP_PROBE_VERSION="$PROBE_VERSION" uv run pytest tests/hw -m hw

# 仮想環境でハーネスの手順を確認する。実機の品質保証とは別。
OEP_HW_BOARDS=virtual-esp32-v003 uv run pytest tests/hw -m hw
```

`OEP_HW_NOFLASH=1` は firmware 書込みを省き、使用した firmware 文字列を記録します。config 試験は設定変更・保存・復元を行い、Wi-Fi 試験は指定したネットワーク設定を保存します。下記の副作用と終了状態を確認し、設備管理者と共有資源の利用を調整してください。現行ハーネスには新設計のホスト共通ロックは未接続です。

複数ボードは `OEP_HW_BOARDS=a,b` でボードごとに順次処理します。候補 source と設置済み firmware の試験を別の結果として残し、使用版の文字列だけで互換性を判定しません。必須契約の skip はリリース確認の未完了です。

## ファイル

| ファイル | 中身 |
|---|---|
| `boards.py` | ボードの表。board-identify の id（USB の probe で id が無いものは unit id）が鍵: 種類、sketch.yaml の profile、焼いた後に OEP の口へ届く方法、期待する model、試験に使う空きチャネル |
| `firmware.py` | firmware の取り方: `OEP_PROBE_DIR`（`arduino-cli compile --profile <profile> --output-dir ...`）か `OEP_PROBE_VERSION`（GitHub のリリースの `firmware-<ver>.json` と image。sha256 を照合。urllib だけ） |
| `flash.py` | ボードの種類ごとの焼き方と、bridge のボードの DTR / RTS によるリセット |
| `record.py` | 1 ボードの一巡: 接続、測ったもの、結果のファイル |
| `test_probe.py` | 試験。この順に回る |
| `test_harness.py` | ハーネス自身の後始末を、プロセス内の仮想ベンチで確かめる（`hw` の印が無いので普段の `uv run pytest` で回る）: 失敗した試験の後と、probe が無くなった後に設定を元に戻す |
| `conftest.py` | `hw` マーカーと `OEP_HW_BOARDS` 無しの skip、ボードごとのまとめ、1 画面の要約 |
| `results/` | `<board>-<firmware>-<client>-<started>.json`。1 巡に 1 つ。名前に巡の開始時刻（`20261006T203121`）を入れ、後の巡が前の巡のファイルを上書きしない（小さな要約だけ。それ以外は `.gitignore` で入れない） |

## 各試験が確かめること

`flash` の後の試験は、firmware を焼けなかったとき、また probe が無くなってまた答えないときは skip になる。それぞれ測ったものを結果のファイルに書き、`verdict` は pytest の
結果。

| 試験 | 確かめること | 記録 |
|---|---|---|
| `flash` | image が入る（esptool / DFU / picotool）。45 秒以内に起動して confirm に答える | 焼く前と後の firmware（describe）、焼いたコマンド、秒数、最後の数行 |
| `identity` | confirm の revision ≥ 1。list に fn 0 の項目が無い（本体は名前を持たない、core §7.2）。clock（core §7.7）の boot_id が confirm と同じ。describe の model が表のとおり。焼いたことで boot_id が変わった。firmware の文字列が焼いた版（ローカルのビルドは `library.properties`、リリースはその版）に等しい | boot_id、firmware、model、unit_id、chip、limits、interface の一覧、clock の uptime_ns と 4 回のうち最短の往復 |
| `required` | どのプローブも出すべきもののうち、ロックなしで確かめられるもの（core §1.2、§7.1、§7.5。oep-spec `docs/conformance.md` 1 節）。`oep dump` が MISSING として出す一覧と同じ：confirm の transport TLV（fn 0 の describe の transport のどれか、または 0xFF を指す）、fn 0 の describe の unit_id、transport、max_op_ms。欠けたものを名指しして失敗する | 一覧（欠けがなければ空） |
| `config` | `oep.probe.config`: label（表の `label` のチャネル、無ければ gpio の 1 本目）と `disable`（`OEP_HW_DISABLE`）を set → get に出て、get の hash が set の hash に等しく、前の hash とは違う（hash は probe 自身のもので、ここでは計算しない）。disable したチャネルへの plan は拒否（unavailable / PinsTaken）。save の前は `needs_save`、save → state が `applied` でその hash、その後は `needs_save` でない。**再起動**（probe が `oep.probe.restart` を出していればそれで: `Host.restart_probe` が restart_max_ms まで待ち、`OEP_HW_REOPEN_S` があればさらにその秒数（下）。USB の probe（P4、RP2）では `OEP_HW_RESTART=1` のときだけで、無ければ飛ばしてその理由を記録に書く。無ければ bridge の classic ESP32 を esptool の hard reset と同じく RTS から EN。どちらも無ければ飛ばす）→ 保存した項目が起動時に適用されている: storage_hash が get の hash に等しく、項目が保存したものと同じ（`same_items`）。両方 unset → 項目が試験前のものと項目ごとに同じ。save。probe にあったほかの設定（bench の slot / bind）はそのまま残す。何が失敗しても、項目と保存は元に戻す（下） | 各段階の hash、再起動のやり方、前後の boot_id と秒数（oep.probe.restart なら restart_max_ms、reopen_s、開き直しが要ったか。戻らなければそのエラー） |
| `wifi` | `oep.probe.config` の wifi の項目（describe にあるとき。probe.config §1.4、§3.3）。`OEP_WIFI_SSID_<n>` / `OEP_WIFI_PASS_<n>`（n は index）があれば: ssid か passphrase の有無が probe のものと違う entry を set し（passphrase は比べられない、host ガイド §15.1）、save する - ベンチの設定なので probe に**残す** -。`OEP_HW_WIFI_WAIT_S`（30 秒）のうちに state のつながりが connected でアドレス付きになる。ほかの経路で試しているボードで DNS-SD が unit_id で見つけたら、`tcp:<unit_id>` で開ける（describe の unit_id を確かめる）。変数が無ければ state を記録するだけ | wifi_max、送った / 同じだった index、save したか、state（state、entry、reason、rssi、アドレスが来たか）、つながるまでの秒数、DNS-SD の port と instance。ssid、passphrase、アドレスは記録しない |
| `wire` | `OEP_HW_TARGET=<name>[@swdio[,swclk]]` のときだけ: scan（名指しの組か全部）、`OEP_HW_RESET=<channel>` なら reset TLV 付きで attach、そのあと 50 回（`OEP_HW_LOOPS`）halt → dmi で s0 / s1 / a0 / a1 → read_block（`OEP_HW_TARGET_ADDR`、既定 0x20000000 から 8 語）→ 同じ 4 本をもう一度、変わっていない → resume | scan の結果、connection、DMSTATUS、速さ、target_id、dpc、回数と秒数、変わったレジスタ: 前、後、読み直し、block の直後の 2 語、dpc（失敗のメッセージにすべての変化を省かずに出す）。op が例外を出したとき: その回、例外、2 回読んだ DMSTATUS と DMCONTROL（生の dmi）、止まっていれば dpc |
| `gpio` | 表の空き 2 チャネル（`OEP_HW_GPIO=a,b`）で `oep.fixture.gpio`: output_high を読むと 1、output_low は 0、input_pullup は 1、input_pulldown は 0。表がその board に空きを与えていなければ skip | 読んだ値すべて |
| `uart` | 表の RX / TX（`OEP_HW_UART=rx,tx`）で、または probe の設定にその plan があるときや表がその board に組を与えていないときはその plan のピンで（どちらも無ければ skip）`oep.fixture.uart`: configure 115200 8N1 が 5 % 以内、status がいま効いている baud と形式（configure の実際の速さ、8N1）を出す、configure 9600。`OEP_HW_UART_LOOP=rx,tx`（2 本を結線）なら write したものが read で戻る | 実際の速さ、status、ループバックのバイト数 |
| `capture` | 表の空き 2 チャネルで `oep.fixture.logic` を、同じピンの `oep.fixture.gpio` と一緒に plan する（ロジックのキャプチャは聞くだけ、oep-if-capture §1.2。共有を断る probe では、代わりに設定の idle 項目で解放したチャネルを引く）: 宣言の最低のレート（1 kHz 以上）で 10 ms の窓（16 サンプル以上。16 KB を超える区画は縮める）のワンショットを、pull-up で 1 回、pull-down で 1 回 → 全チャネルの全サンプルが 1、次に 0。区画のサンプル数が configure の値に等しい、窓（1 % を引いた分）より早く終わらず、窓の後 1 秒以内に終わる、status が done で dropped / slipped の flag 無し、start ごとに世代が 1 進む | 宣言（rate_range、max_samples 付きの mode、channels の max）、実際のレート、layout（w、pos）、サンプル数、バイト数、blocking_ms、キャプチャごとに start → done の秒数、読み出しの秒数、start_ns とその不確かさ、世代、チャネルごとの 1 の数 |
| `capture_analog` | `oep.fixture.analog`（list にあれば）を、役割 0 が許す空きチャネル（`OEP_HW_ANALOG=<channel>`）で: 最低のレート、10 ms、いちばん広い frontend のワンショット。layout（s / o / b、order）、frontend_used、scale / zero / reference が返り、値が b ビットに収まる。pull-up / pull-down の帯（平均がフルスケールの 80 % 以上 / 20 % 以下）は probe が gpio とピンを共有させるときだけ判定する。参照の ESP32 firmware は共有させない（アナログのチャネルは何とも共有しない）ので、浮いたピンの値を記録する | 宣言（frontend も）、configure の応答、値の最小 / 最大 / 平均（生の値と mV）、calibration（scheme、vrefint） |
| `capture_group` | `oep.fixture.capture-group`（両方のトラックを宣言していれば）: 上と同じ設定のロジックとアナログを bind して一緒に start → start の応答に両方の世代、各トラックに configure どおりのサンプル数の区画が 1 つ、各 start_ns が組の start_ns 以降（1 秒以内）、両方読み出す、解く | 組の宣言（tracks だけ）、start_ns、各トラックのそこからのずれ、世代、done までの秒数 |
| `i2c_target` | `oep.fixture.i2c-target`（list にあれば）を、役割が許す空きチャネルの SDA / SCL（`OEP_HW_I2C=sda,scl`）で: configure 前は state 0。address 0x42 で configure → state 1、置き場は無い。3 つまで（queue_depth まで）preload → tx_slots がその数。read_rx（記録する）。ops にあれば stretch(0)。もう一度 configure → state 1、列、置き場、累計が 0。バスに controller は無く線は浮いているので、グリッチによる誤りやフレームは記録して判定しない | 宣言（queue_depth、max_clock_hz、max_length、features、max_stretch_us、internal_pullups）、各 status、read_rx、stretch |
| `spi_target` | `oep.fixture.spi-target`（list にあれば）を、空き 4 チャネルの SCK / MOSI / MISO / CS（`OEP_HW_SPI=sck,mosi,miso,cs`。固定のピンの組で来る interface は最初の channel_group）で: configure 前は state 0。mode 0 MSB 先で configure → state 1。4 byte を MISO 2 byte 付きで arm → armed、または消費済み（SCK / CS が浮いていて、ATOM の target は arm の直後に 1〜2 の幻の転送を数える。2 つ目の arm の「1 回に 1 つ」の拒否はそのため当てにしない）。read_rx。もう一度 configure（mode 0 MSB 先。reset は無い）→ state 1、armed が消え、列と累計が 0 | 宣言、各 status（transactions = 幻の転送）、read_rx |
| `console` | `OEP_HW_TARGET` のときだけ: scan、走らせたまま attach（halt も reset も無し）、その connection に `oep.target.console` を mechanism dmseq（無ければ probe が宣言する最初のもの）で open、open 時の位置から 1 秒間届くものを読む、streams の一覧にそのストリームが出る、close、detach | mechanism の一覧、stream、existing、溜まっていたバイト数、見えたバイト数（0 もある）、lost、mark の数、streams の一覧、先頭 64 byte |
| `port_speed` | UART bridge で、`oep.probe.link` の ops に port_speed がある probe だけ: `oep_client.linktest.matrix` を今の速さと表の候補（`OEP_HW_RATES`）で、in / out / duplex、同時 1 と probe の最大、1 つのフレームの大きさ（`max_frame - 7`、source の 1 つの応答が運ぶ最大。sink の要求は多くても `max_frame - 12`）、1 条件 `OEP_HW_FRAMES`（100）フレーム。**判定**（host 開発ガイド §17.3.2）: 上げた速さの 1 つずつの条件で、broken + lost が 3 以上かつ割合が max(起動時の速さの同じ条件の割合 × 2, 5 %（`OEP_HW_ERROR_MAX`）) を超えたら落とす。通る候補が 1 つも無いときだけ落とす（落ちた候補、probe が断った候補、confirm が返らない候補は記録する） | 条件ごとの ok / broken / lost、KB/s、秒数、速さの実際の値、link のカウンタ |
| `session` | 1000 ms の lease が切れる → `NoSession`（解放され、再開は無い）。新しい id の force → 古い id は `Locked`。その end の後はどの id も `NoSession` | lease、boot_id、拒否の文 |

結果のファイルにはほかに、クライアントの版と commit、firmware の出所（checkout + commit + dirty か、リリースの版 + json の
URL + sha256）、ホストの platform、その回の `OEP_*` の環境変数すべて（`OEP_WIFI_*` は `(set)` とだけ。値は入れない）が入る。1 画面の要約は pytest の最後に出る。

仮想ベンチ（`virtual-esp32-v003`）では `capture` はレベルを記録するだけで判定しない（仮想ベンチはピンではなくカウンタを取り、
ワンショットは start の応答と同時に終わる）。

## 戻らない probe と、tests/hw が変える設定

**再起動の待ち。** host が再起動した probe を待って繰り返すのは、restart_max_ms が過ぎるまでである（host ガイド §5.2）:
`restart_probe` は自分からはそれより長く待たず、それまでに戻らない probe は無くなったものとして link を閉じる。WSL では、列挙し
直した USB の probe は Windows では新しい device で、`usbipd attach --wsl` が付け直して初めて Linux に届く（手で、または
Windows のシェルで `usbipd attach --wsl --auto-attach --busid <busid>` を動かしたままにしておく）。それは probe の
restart_max_ms（RP2350 では 2.0 s）より長く掛かりうる。`OEP_HW_REOPEN_S=<秒>` は、そうした probe を利用者が開き直すことを前もって
頼むもの: 待ちが過ぎた後、新しく開くのと同じに（最初は confirm）さらにその秒数まで開き直す（`restart_probe(reopen_s=...)`。
記録の `reopened`）。無ければ config の試験はそれを書いて失敗し、後の試験は「the probe is gone」で skip になる - どれも最初に
1 度開き直してみるので、走っている間に手で attach すれば残りは戻る。

**設定。** 試験が probe の設定を変えるのは 2 か所で、何があっても（assert の失敗、例外、probe が無くなる）元に戻す:

| 場所 | 変えるもの | 戻し方 |
|---|---|---|
| `wifi` | `OEP_WIFI_*` からの `wifi` の項目（set と save） | 戻さない: ベンチの設定（消すなら `oep config wifi-unset`） |
| `config` | `label` と `disable` の項目（set）、保存（2 回 save） | 試験が両方 unset して save する。そのうえで `finally` が `Run.restore_settings` を呼ぶ: 項目は取り除くか、走る前の値に set し直し、保存はもう一度 save する（走る前に何も保存されていなければ erase） |
| `capture`（logic。probe が capture の pin に gpio を重ねるのを断るとき） | 2 本のチャネルの `idle` 項目（set、保存しない） | `_Pull.release` が unset し、その `finally` で `restore_settings` |
| `gpio`、`uart`、`capture*`、`i2c_target`、`spi_target` | plan（oep.probe.plan）、セッションの状態 | それぞれの `finally` で `plan_release`。plan はセッションのものなので、失敗で残ったものも走り終わりのセッションの終わりで解放される |
| `wire`、`console` | connection、console のストリーム | `finally` で detach（と close）。これもセッションのもの |
| `capture_group` | group の track の bind | `finally` で `bind([])`。これもセッションのもの |
| `port_speed` | link の速さ | `linktest.matrix` が `finally` で起動時の速さに戻す。probe も何もしなければ自分で戻る |
| `session` | lease、force での取り上げ | 自分のセッションで、終える |

ハーネスは `link.open_host` で probe を開くので、probe ごとにセッションの id を残す（README の「走り直す host」）: 途中で止めた実行は
probe にセッションを残す（経路が閉じてもセッションは終わらない、transports §3）が、次の実行の最初の open がそれを終え、30 s の lease
が切れるのを待たない。

設定の slot、bind、uart、plan の項目にはどの試験も触れない: bench が持たせているものは変えない。probe が無くなったときは、
`restore_settings` が開き直し（`OEP_HW_REOPEN_S`、少なくとも 5 s 待ち、無くなったセッションの lease を 35 s まで待つ）、
走り終わりの後始末でもう 1 度試す。それでも戻せなかったものは結果のファイル（`_settings.left_on_probe`）に書き、まとめに
**SETTINGS LEFT ON THE PROBE** として、probe が戻ってから打つコマンドと一緒に出す。例:

```sh
oep config remove <probe> disable 28
oep config save <probe>        # 走る前に何も保存されていなければ `oep config erase <probe>`
```

## 対象と更新経路

現行の `boards.py` は個体と profile・配線既定値を束ねた legacy 表です。個体表は外部設定へ移す改修対象であり、新規利用者が汎用 TOML を渡せる状態にはまだなっていません。実設備の配置、観測結果、serial、USB topology は各設備管理者が別に管理します。

| 種類 | 現行の更新経路 |
|---|---|
| classic ESP32 | esptool と merged image |
| ESP32-P4 | USB DFU の app image、または明示した USB-Serial/JTAG 経路 |
| RP2040 / RP2350 | BOOTSEL と picotool、または明示した UF2 drive |
| TCP | この入口では更新しない。指定されたネットワーク経路で設置済み firmware を検証 |

TCP は `OEP_HW_BOARDS=tcp:<unit_id>` または `tcp://<host>:<port>` を明示できます。汎用 TCP 指定では期待 model や空き channel が未設定です。配線確認なく fixture の channel を追加しません。`OEP_HW_NOFLASH` は設置済み firmware の試験を選び、設定や power 操作の禁止を意味しません。

## 環境変数

| 変数 | 意味 |
|---|---|
| `OEP_HW_BOARDS` | ボード id のコンマ区切り（必須。無ければ何も回らない）。`tcp:<unit_id>` / `tcp://<host>[:<port>]` は表に無い TCP の probe |
| `OEP_WIFI_SSID_<n>`、`OEP_WIFI_PASS_<n>` | `wifi` の試験が使うベンチの Wi-Fi（n は entry の index。PASS が無ければ開いたネットワーク）。環境変数から読み、表示せず、`(set)` とだけ記録する。`oep config wifi <probe> --from-env` も同じ変数を読む |
| `OEP_HW_WIFI_WAIT_S` | `wifi` の試験がつながりを待つ秒数（既定 30） |
| `OEP_HW_ATOM_TCP` | `esp32-pico-d4-50029191fe34-tcp` の TCP の行き先（既定 `tcp:50029191fe34`、DNS-SD） |
| `OEP_PROBE_DIR` / `OEP_PROBE_VERSION` / `OEP_HW_NOFLASH` | firmware の出所（どれか 1 つ） |
| `OEP_HW_TARGET`、`OEP_HW_RESET`、`OEP_HW_TARGET_ADDR`、`OEP_HW_LOOPS` | wire の試験: target が繋がっている（どこに）、reset の線、block の番地、回数 |
| `OEP_HW_REOPEN_S` | 再起動した probe を、restart_max_ms が過ぎた後さらに何秒まで開き直すか（利用者が開き直す。上。WSL / usbipd）。既定 0: しない - restart_max_ms の後 probe は無くなったものとする |
| `OEP_HW_RESTART=1` | config の試験で USB の probe（P4、RP2）を `oep.probe.restart` で再起動する（既定はしない: oep-probe-arduino 0.0.29-dev+3c0cd99 では RP2350 と P4 が再起動の後に列挙に失敗し、抜き差しが要った） |
| `OEP_HW_WIRE` | wire / console の試験の wire の名前（既定: `OEP_HW_TARGET` が 1 本のピンを名指し、probe が `oep.wire.swio` を持てばそれ、無ければ最初の wire）。console の試験は、その connection の open を受ける `oep.target.console` の instance を使う |
| `OEP_HW_GPIO`、`OEP_HW_DISABLE`、`OEP_HW_UART`、`OEP_HW_UART_LOOP` | fixture の試験のチャネルの上書き |
| `OEP_HW_ANALOG`、`OEP_HW_I2C`、`OEP_HW_SPI` | アナログのキャプチャのチャネル、i2c-target の `sda,scl`、spi-target の `sck,mosi,miso,cs`（既定: 表の空きチャネル（gpio の 2 本、disable、UART の 2 本）のうち describe が許すもの） |
| `OEP_HW_RATES`、`OEP_HW_FRAMES`、`OEP_HW_LT_TIMEOUT`、`OEP_HW_ERROR_MAX` | port_speed の候補、1 条件のフレーム数、答えの待ち、判定の床（既定 5 %） |
| `OEP_HW_PICOTOOL`、`OEP_HW_UF2_DRIVE`、`OEP_HW_USBIP_BUSID`、`OEP_HW_CACHE` | ツールと環境の細部 |

ツール: `arduino-cli`（ローカルのビルド）、`esptool`（classic ESP32）、`picotool`（RP2）。DFU と boot ROM の探索は pyusb
（クライアントの依存に既にある）。`gh`、`dfu-util`、マウントしたドライブは要らない。
