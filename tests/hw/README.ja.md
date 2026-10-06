# 実機の結合試験（`tests/hw`）

[English](README.md)

oep-spec の [docs/release-testing.ja.md](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/release-testing.ja.md)
のリリース前の試験。probe の firmware（oep-probe-arduino の `examples/Firmware/OepProbe`）を実機に焼き、このクライアントで
一通り動かす。普段の試験（`uv run pytest`）は偽の probe に対して回り、実機には触れない。このディレクトリは `OEP_HW_BOARDS`
でボードを名指ししない限り丸ごと skip になる。firmware のリリース前（main をビルドして手元のボード全部）とクライアントの
リリース前（最新の firmware のリリース）の両方で回し、通らなければどちらもリリースしない。

```sh
# checkout をビルドして焼く（main どうし: firmware のリリース前）
OEP_HW_BOARDS=esp32-pico-d4-50029191fe34 OEP_PROBE_DIR=~/dev_oep/oep-probe-arduino uv run pytest tests/hw -m hw

# GitHub のリリースの image（クライアントのリリース前、再現、ボードのある CI）
OEP_HW_BOARDS=esp32-pico-d4-50029191fe34 OEP_PROBE_VERSION=0.0.25 uv run pytest tests/hw -m hw

# ボードに入っている firmware をそのまま試す（焼かない）
OEP_HW_BOARDS=9489dd2ae0953650 OEP_HW_NOFLASH=1 uv run pytest tests/hw -m hw

# 偽の probe（pty）で試験の手順だけ通す（実機なし。リリースの試験にはならない）
OEP_HW_BOARDS=fake-esp32-v003 uv run pytest tests/hw -m hw
```

`-s` を付けると焼く進み具合と port_speed の表がその場で出る。1 ボードの一巡は 1.5〜2.5 分（ESP32 を 115200 で焼く約 40 秒を
含む）。ローカルのビルドは profile ごとに初回だけ約 1 分足す（`~/.cache/oep-hw/` に残る。`OEP_HW_CACHE`）。複数のボードは
`OEP_HW_BOARDS=a,b` で、ボードごとに順に回る。

## ファイル

| ファイル | 中身 |
|---|---|
| `boards.py` | ボードの表。board-identify の id（USB の probe で id が無いものは unit id）が鍵: 種類、sketch.yaml の profile、焼いた後に OEP の口へ届く方法、期待する model、試験に使う空きチャネル |
| `firmware.py` | firmware の取り方: `OEP_PROBE_DIR`（`arduino-cli compile --profile <profile> --output-dir ...`）か `OEP_PROBE_VERSION`（GitHub のリリースの `firmware-<ver>.json` と image。sha256 を照合。urllib だけ） |
| `flash.py` | ボードの種類ごとの焼き方と、bridge のボードの DTR / RTS によるリセット |
| `record.py` | 1 ボードの一巡: 接続、測ったもの、結果のファイル |
| `test_probe.py` | 試験。この順に回る |
| `conftest.py` | `hw` マーカーと `OEP_HW_BOARDS` 無しの skip、ボードごとのまとめ、1 画面の要約 |
| `results/` | `<board>-<firmware>-<client>-<started>.json`。1 巡に 1 つ。名前に巡の開始時刻（`20261006T203121`）を入れ、後の巡が前の巡のファイルを上書きしない（小さな要約だけ。それ以外は `.gitignore` で入れない） |

## 各試験が確かめること

`flash` の後の試験は、firmware を焼けなかったら skip になる。それぞれ測ったものを結果のファイルに書き、`verdict` は pytest の
結果。

| 試験 | 確かめること | 記録 |
|---|---|---|
| `flash` | image が入る（esptool / DFU / picotool）。45 秒以内に起動して confirm に答える | 焼く前と後の firmware（describe）、焼いたコマンド、秒数、最後の数行 |
| `identity` | confirm の revision ≥ 1。list に fn 0 の項目が無い（本体は名前を持たない、core §7.2）。clock（core §7.7）の boot_id が confirm と同じ。describe の model が表のとおり。焼いたことで boot_id が変わった。firmware の文字列が焼いた版（ローカルのビルドは `library.properties`、リリースはその版）に等しい | boot_id、firmware、model、unit_id、chip、limits、interface の一覧、clock の uptime_ns と 4 回のうち最短の往復 |
| `required` | どのプローブも出すべきもののうち、ロックなしで確かめられるもの（core §1.2、§7.1、§7.5。oep-spec `docs/conformance.md` 1 節）。`oep dump` が MISSING として出す一覧と同じ：confirm の transport TLV（fn 0 の describe の transport のどれか、または 0xFF を指す）、fn 0 の describe の unit_id、transport、max_op_ms、discoverable（0 でも出す）。欠けたものを名指しして失敗する | 一覧（欠けがなければ空） |
| `config` | `oep.probe.config`: label（表の `label` のチャネル、無ければ gpio の 1 本目）と `disable`（`OEP_HW_DISABLE`）を set → get に出て `hash_of` が一致。disable したチャネルへの plan は拒否（unavailable / PinsTaken）。save → state が `applied` でその hash。**再起動**（probe が `oep.probe.restart` を出していればそれで: `Host.restart_probe` が restart_max_ms まで待つ。USB の probe（P4、RP2）では `OEP_HW_RESTART=1` のときだけで、無ければ飛ばしてその理由を記録に書く。無ければ bridge の classic ESP32 を esptool の hard reset と同じく RTS から EN。どちらも無ければ飛ばす）→ 保存した項目が起動時に適用されている（state、get）。両方 unset して save → 試験前の hash に戻る。probe にあったほかの設定（bench の slot / bind）はそのまま残す | 各段階の hash、再起動のやり方、前後の boot_id と秒数（oep.probe.restart なら restart_max_ms） |
| `wire` | `OEP_HW_TARGET=<name>[@swdio[,swclk]]` のときだけ: scan（名指しの組か全部）、`OEP_HW_RESET=<channel>` なら reset TLV 付きで attach、そのあと 50 回（`OEP_HW_LOOPS`）halt → dmi で s0 / s1 / a0 / a1 → read_block（`OEP_HW_TARGET_ADDR`、既定 0x20000000 から 8 語）→ 同じ 4 本をもう一度、変わっていない → resume | scan の結果、connection、DMSTATUS、速さ、target_id、dpc、回数と秒数、変わったレジスタ: 前、後、読み直し、block の直後の 2 語、dpc（失敗のメッセージにすべての変化を省かずに出す） |
| `gpio` | 表の空き 2 チャネル（`OEP_HW_GPIO=a,b`）で `oep.fixture.gpio`: output_high を読むと 1、output_low は 0、input_pullup は 1、input_pulldown は 0。表がその board に空きを与えていなければ skip | 読んだ値すべて |
| `uart` | 表の RX / TX（`OEP_HW_UART=rx,tx`）で、または probe の設定にその plan があるときや表がその board に組を与えていないときはその plan のピンで（どちらも無ければ skip）`oep.fixture.uart`: configure 115200 8N1 が 5 % 以内、status が `session` でその速さと形式、configure 9600。`OEP_HW_UART_LOOP=rx,tx`（2 本を結線）なら write したものが read で戻る | 実際の速さ、status、ループバックのバイト数 |
| `capture` | 表の空き 2 チャネルで `oep.fixture.logic` を、同じピンの `oep.fixture.gpio` と一緒に plan する（ロジックのキャプチャは聞くだけ、oep-if-capture §1.2。共有を断る probe では、代わりに設定の idle 項目で解放したチャネルを引く）: 宣言の最低のレート（1 kHz 以上）で 10 ms の窓（16 サンプル以上。16 KB を超える区画は縮める）のワンショットを、pull-up で 1 回、pull-down で 1 回 → 全チャネルの全サンプルが 1、次に 0。区画のサンプル数が configure の値に等しい、窓（宣言のレートの不確かさを引いた分）より早く終わらず、窓の後 1 秒以内に終わる、status が done で dropped / slipped の flag 無し、start ごとに世代が 1 進む | 宣言（rate_range、mode、layout、max_read、segment_ring）、実際のレート、layout（w、pos）、サンプル数、バイト数、timing（jitter、ppm、blocking）、キャプチャごとに start → done の秒数、読み出しの秒数、start_ns とその不確かさ、世代、チャネルごとの 1 の数 |
| `capture_analog` | `oep.fixture.analog`（list にあれば）を、役割 0 が許す空きチャネル（`OEP_HW_ANALOG=<channel>`）で: 最低のレート、10 ms、いちばん広い frontend のワンショット。layout（s / o / b、order）、frontend_used、scale / zero / reference が返り、値が b ビットに収まる。pull-up / pull-down の帯（平均がフルスケールの 80 % 以上 / 20 % 以下）は probe が gpio とピンを共有させるときだけ判定する。参照の ESP32 firmware は共有させない（アナログのチャネルは何とも共有しない）ので、浮いたピンの値を記録する | 宣言（frontend も）、configure の応答、値の最小 / 最大 / 平均（生の値と mV）、calibration（scheme、vrefint） |
| `capture_group` | `oep.fixture.capture-group`（両方のトラックを宣言していれば）: 上と同じ設定のロジックとアナログを bind して一緒に start → start の応答に両方の世代、各トラックに configure どおりのサンプル数の区画が 1 つ、各 start_ns が組の start_ns 以降（1 秒以内）、両方読み出す、解く | 組の宣言、start_ns、各トラックのそこからのずれ、世代、done までの秒数 |
| `i2c_target` | `oep.fixture.i2c-target`（list にあれば）を、役割が許す空きチャネルの SDA / SCL（`OEP_HW_I2C=sda,scl`）で: configure 前は state 0。address 0x42 mode 3（宣言があれば）で configure → state 1、tx を 3 つ preload → 1、2、3 と数え tx_slots 3。mode 1 で configure、arm_rx 4 → armed。read_rx。reset → armed が消え、列と累計が 0。バスに controller は無く線は浮いているので、グリッチによる誤りやフレームは記録して判定しない | 宣言（queue_depth、max_clock_hz、max_length、features）、各 status、read_rx |
| `spi_target` | `oep.fixture.spi-target`（list にあれば）を、空き 4 チャネルの SCK / MOSI / MISO / CS（`OEP_HW_SPI=sck,mosi,miso,cs`。固定のピンの組で来る interface は最初の channel_group）で: configure 前は state 0。mode 0 MSB 先で configure → state 1。4 byte を MISO 2 byte 付きで arm → armed、または消費済み（SCK / CS が浮いていて、ATOM の target は arm の直後に 1〜2 の幻の転送を数える。2 つ目の arm の「1 回に 1 つ」の拒否はそのため当てにしない）。read_rx。reset → armed が消え、列と累計が 0 | 宣言、各 status（transactions = 幻の転送）、read_rx |
| `console` | `OEP_HW_TARGET` のときだけ: scan、走らせたまま attach（halt も reset も無し）、その connection に `oep.target.console` を mechanism dmseq（無ければ probe が宣言する最初のもの）で open、open 時の位置から 1 秒間届くものを読む、streams の一覧にそのストリームが出る、close、detach | mechanism の一覧、stream、existing、溜まっていたバイト数、見えたバイト数（0 もある）、lost、mark の数、streams の一覧、先頭 64 byte |
| `port_speed` | UART bridge で、`oep.probe.link` の ops に port_speed がある probe だけ: `oep_client.linktest.matrix` を今の速さと表の候補（`OEP_HW_RATES`）で、in / out / duplex、同時 1 と probe の最大、1 フレーム（`max_frame - 26`、source の 1 つの応答が運ぶ最大）、1 条件 `OEP_HW_FRAMES`（100）フレーム。**判定**（host 開発ガイド §17.3.2）: 上げた速さの 1 つずつの条件で、broken + lost が 3 以上かつ割合が max(起動時の速さの同じ条件の割合 × 2, 5 %（`OEP_HW_ERROR_MAX`）) を超えたら落とす。通る候補が 1 つも無いときだけ落とす（落ちた候補、probe が断った候補、confirm が返らない候補は記録する） | 条件ごとの ok / broken / lost、KB/s、秒数、速さの実際の値、link のカウンタ |
| `session` | 1000 ms の lease が切れる → `NoSession`（解放され、再開は無い）。新しい id の force → 古い id は `Locked`。その end の後はどの id も `NoSession` | lease、boot_id、拒否の文 |

結果のファイルにはほかに、クライアントの版と commit、firmware の出所（checkout + commit + dirty か、リリースの版 + json の
URL + sha256）、ホストの platform、その回の `OEP_*` の環境変数すべてが入る。1 画面の要約は pytest の最後に出る。

偽の probe（`fake-esp32-v003`）では `capture` はレベルを記録するだけで判定しない（偽の probe はピンではなくカウンタを取り、
ワンショットは start の応答と同時に終わる）。

## ボードと焼き方

| ボード（`OEP_HW_BOARDS` の id） | 種類 | profile | 焼き方 | 実施 |
|---|---|---|---|---|
| `esp32-pico-d4-50029191fe34` M5Stack ATOM（ESP32-PICO-D4、FTDI） | esp32 | esp32 | `esptool --chip esp32 -p <port> -b 115200 write-flash 0x0 <merged.bin>`（ATOM の bridge は 115200） | **済**（main の 0.0.26 ビルドとリリース 0.0.25、2026-10-01） |
| `esp32-d0wd-v3-0070070d9394` V003 のジグ（ESP32-D0WD-V3、CH340） | esp32 | esp32 | 同上 | **済**（main の 0.0.26、2026-10-02。SWIO 越しの CH32V003 で wire 50 往復 pass。port_speed は CH340 のまとまった落ちで判定が揺れる）。出力できるチャネルは SWIO / NRST 以外すべて V003 のパッドにつながる（ArduinoCore-CH32RV の `tests/benches/v003-esp32.toml`）: 表が与えるのは入力専用の 34 / 35（config の disable / label、アナログのキャプチャ）だけなので、gpio / capture / capture_group は skip、uart はジグの設定の plan で走る |
| `esp32-series-30eda0e31108` X035 のジグ（ESP32-P4）、`esp32-series-30eda0e343c6` 2 枚目の P4 | esp32p4 | esp32p4 | 動いている probe へ app の `.bin` を USB DFU 1.1 で（pyusb。`dfu-util -D` と同じ: DFU の interface を class FE/01 で探し、wTransferSize は functional descriptor から、DNLOAD をブロックごと、長さ 0 の DNLOAD で manifest）、device が消えて戻るのを待つ。WSL では `usbipd.exe attach --wsl --busid <busid>`（表の `usbip_busid`） | **済**（X035 治具、main の 0.0.26、2026-10-02: DFU は interface 4 / 4096 B で 8 s。RVSWD 越しの wire 50 往復 pass。uart は治具の設定の plan rx 12 / tx 6 で試す）。2 枚目の P4 は手で DFU（旧 firmware では interface 7 / 1024 B）で焼けたが、USB serial が変わり Windows の usbipd の再 bind が要る |
| `esp32-series-30eda0ea068b` 3 枚目の P4（ESP32-P4、FS の USB-Serial/JTAG の口だけ。こちらのもの）: CH32V003 が SWIO 19、NRST 4、電源は GPIO5（設定の idle 5 output-high が保つ） | esp32p4-usj | esp32p4 | その口から `esptool --chip esp32p4 -p <port> erase-region 0xe000 0x2000`（otadata: app0 で起動）、次に `write-flash 0x10000 <app.bin>`。DFU と同じく app だけを書くので、保存した設定（target の電源）が残る | **済**（main 0f013b7、2026-10-02: `OEP_HW_TARGET=ch32v003@19 OEP_HW_RESET=4 OEP_HW_ANALOG=20`。13 pass、port_speed は skip。probe の起動で電源が入った V003 は 0.1 秒ほど何も答えない: wire / console の scan は 2 秒までやり直す）|
| `9489dd2ae0953650` SparkFun Pro Micro RP2350（1209:4F45。unit id が鍵） | rp2 | promicrorp2350 | CDC の口へ 1200 baud のタッチ（BOOTSEL）、boot ROM の USB device（`RP2350 Boot` / `RP2 Boot`。product と serial で選ぶので、ホストのほかの RP2 には触れない）を待ち、`picotool load -x <uf2> --bus --address`（`OEP_HW_PICOTOOL`、既定は PATH の `picotool`。[picotool 2.3.1 Linux x86_64](https://github.com/raspberrypi/pico-sdk-tools/releases/download/v2.3.1-0/picotool-2.3.1-x86_64-lin.tar.gz)）、CDC の口が戻るのを 45 秒まで待つ。`OEP_HW_UF2_DRIVE=<mount>` なら代わりにその BOOTSEL のドライブへ `.uf2` を置く（ドライブをマウントできるホスト）。どちらもできなければ焼かずに、入っている firmware で試験を続ける | **済**（picotool、2026-10-02。最初の一巡は UART が使えないピンで `uart` の途中に probe が固まった → oep-probe-arduino 654b06a で修正、そのビルドでは全部 pass）|

ATOM の変換（CH552 の FTDI 互換）は probe→host をまとめて落とす（ある分は 0 %、次の分は 10〜55 %、2026-10-02）: ATOM での port_speed の失敗は 1 回やり直してから数える。port_speed の門としては CH340 の治具のほうが安定している。

`OEP_HW_NOFLASH=1` はどのボードでも焼かずに、入っている firmware を試す（"on-board" として記録。firmware の文字列は記録する
だけで照合しない）。

`rp2040` / `rp2350`（Pico）の profile も同じ rp2 の焼き方で、ボードの表に行を足せばよい。

## 共有のジグ: 使うときの決まり

V003 のジグ、X035 のジグ（ESP32-P4）、WCH-Link はこのホストでは ArduinoCore-CH32RV の bench（別のセッションの機材）のもの。
リリースの試験で網羅できるようボードの表には載せてあるが、**ジグで回すのは bench の許可を待ってから**: 毎回まず聞く。渡されて
いない device は焼かない、リセットしない。ATOM（`/dev/ttyUSB1`）と 2 枚目の P4（`esp32-series-30eda0e343c6`）が OEP の試験に自由に使えるボード。表の `notes` に `jig` と
あるものが共有（`Board.shared`）。

## 環境変数

| 変数 | 意味 |
|---|---|
| `OEP_HW_BOARDS` | ボード id のコンマ区切り（必須。無ければ何も回らない） |
| `OEP_PROBE_DIR` / `OEP_PROBE_VERSION` / `OEP_HW_NOFLASH` | firmware の出所（どれか 1 つ） |
| `OEP_HW_TARGET`、`OEP_HW_RESET`、`OEP_HW_TARGET_ADDR`、`OEP_HW_LOOPS` | wire の試験: target が繋がっている（どこに）、reset の線、block の番地、回数 |
| `OEP_HW_RESTART=1` | config の試験で USB の probe（P4、RP2）を `oep.probe.restart` で再起動する（既定はしない: oep-probe-arduino 0.0.29-dev+3c0cd99 では RP2350 と P4 が再起動の後に列挙に失敗し、抜き差しが要った） |
| `OEP_HW_WIRE` | wire / console の試験の wire の名前（既定: `OEP_HW_TARGET` が 1 本のピンを名指し、probe が `oep.wire.swio` を持てばそれ、無ければ最初の wire）。console の試験は、その connection の open を受ける `oep.target.console` の instance を使う |
| `OEP_HW_GPIO`、`OEP_HW_DISABLE`、`OEP_HW_UART`、`OEP_HW_UART_LOOP` | fixture の試験のチャネルの上書き |
| `OEP_HW_ANALOG`、`OEP_HW_I2C`、`OEP_HW_SPI` | アナログのキャプチャのチャネル、i2c-target の `sda,scl`、spi-target の `sck,mosi,miso,cs`（既定: 表の空きチャネル（gpio の 2 本、disable、UART の 2 本）のうち describe が許すもの） |
| `OEP_HW_RATES`、`OEP_HW_FRAMES`、`OEP_HW_LT_TIMEOUT`、`OEP_HW_ERROR_MAX` | port_speed の候補、1 条件のフレーム数、答えの待ち、判定の床（既定 5 %） |
| `OEP_HW_PICOTOOL`、`OEP_HW_UF2_DRIVE`、`OEP_HW_USBIP_BUSID`、`OEP_HW_CACHE` | ツールと環境の細部 |

ツール: `arduino-cli`（ローカルのビルド）、`esptool`（classic ESP32）、`picotool`（RP2）。DFU と boot ROM の探索は pyusb
（クライアントの依存に既にある）。`gh`、`dfu-util`、マウントしたドライブは要らない。
