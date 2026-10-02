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
| `results/` | `<board>-<firmware>-<client>.json`。1 巡に 1 つ（小さな要約だけ。それ以外は `.gitignore` で入れない） |

## 各試験が確かめること

`flash` の後の試験は、firmware を焼けなかったら skip になる。それぞれ測ったものを結果のファイルに書き、`verdict` は pytest の
結果。

| 試験 | 確かめること | 記録 |
|---|---|---|
| `flash` | image が入る（esptool / DFU / picotool）。45 秒以内に起動して confirm に答える | 焼く前と後の firmware（describe）、焼いたコマンド、秒数、最後の数行 |
| `identity` | confirm の revision ≥ 1。list の先頭が `oep.core`。describe の model が表のとおり。焼いたことで boot_id が変わった。firmware の文字列が焼いた版（ローカルのビルドは `library.properties`、リリースはその版）に等しい | boot_id、firmware、model、unit_id、chip、limits、interface の一覧 |
| `config` | `oep.probe.config`: label と `disable` を set → get に出て `hash_of` が一致。disable したチャネルへの plan は拒否（unavailable / PinsTaken）。save → state が `applied` でその hash。**再起動**（bridge の classic ESP32: esptool の hard reset と同じく RTS から EN）→ 保存した項目が起動時に適用されている（state、get）。両方 unset して save → 試験前の hash に戻る。probe にあったほかの設定（bench の slot / bind）はそのまま残す | 各段階の hash、再起動の前後の boot_id と秒数 |
| `wire` | `OEP_HW_TARGET=<name>[@swdio[,swclk]]` のときだけ: scan（名指しの組か全部）、`OEP_HW_RESET=<channel>` なら reset TLV 付きで attach、そのあと 50 回（`OEP_HW_LOOPS`）halt → dmi で s0 / s1 / a0 / a1 → read_block（`OEP_HW_TARGET_ADDR`、既定 0x20000000 から 8 語）→ 同じ 4 本をもう一度、変わっていない → resume | scan の結果、connection、DMSTATUS、速さ、target_id、dpc、回数と秒数、変わったレジスタ |
| `gpio` | 表の空き 2 チャネル（`OEP_HW_GPIO=a,b`）で `oep.fixture.gpio`: output_high を読むと 1、output_low は 0、input_pullup は 1、input_pulldown は 0 | 読んだ値すべて |
| `uart` | 表の RX / TX（`OEP_HW_UART=rx,tx`）で `oep.fixture.uart`: configure 115200 8N1 が 5 % 以内、status が `session` でその速さと形式、configure 9600。`OEP_HW_UART_LOOP=rx,tx`（2 本を結線）なら write したものが read で戻る | 実際の速さ、status、ループバックのバイト数 |
| `port_speed` | UART bridge の probe だけ: `oep_client.linktest.matrix` を今の速さと表の候補（`OEP_HW_RATES`）で、in / out / duplex、同時 1 と probe の最大、1 フレーム（`max_frame - 16`）、1 条件 `OEP_HW_FRAMES`（100）フレーム。**判定**（host 開発ガイド §7.3.2）: 上げた速さの 1 つずつの条件で、broken + lost が 3 以上かつ割合が max(起動時の速さの同じ条件の割合 × 2, 5 %（`OEP_HW_ERROR_MAX`）) を超えたら落とす。通る候補が 1 つも無いときだけ落とす（落ちた候補、probe が断った候補、confirm が返らない候補は記録する） | 条件ごとの ok / broken / lost、KB/s、秒数、速さの実際の値、link のカウンタ |
| `session` | 1000 ms の lease が切れる → `Expired`。同じ id で open し直すと resumed 2（swept）。新しい id の force → 古い id は `Locked` | lease、resumed の値、拒否の文 |

結果のファイルにはほかに、クライアントの版と commit、firmware の出所（checkout + commit + dirty か、リリースの版 + json の
URL + sha256）、ホストの platform、その回の `OEP_*` の環境変数すべてが入る。1 画面の要約は pytest の最後に出る。

## ボードと焼き方

| ボード（`OEP_HW_BOARDS` の id） | 種類 | profile | 焼き方 | 実施 |
|---|---|---|---|---|
| `esp32-pico-d4-50029191fe34` M5Stack ATOM（ESP32-PICO-D4、FTDI） | esp32 | esp32 | `esptool --chip esp32 -p <port> -b 115200 write-flash 0x0 <merged.bin>`（ATOM の bridge は 115200） | **済**（main の 0.0.26 ビルドとリリース 0.0.25、2026-10-01） |
| `esp32-d0wd-v3-0070070d9394` V003 のジグ（ESP32-D0WD-V3、CH340） | esp32 | esp32 | 同上 | **済**（main の 0.0.26、2026-10-02。SWIO 越しの CH32V003 で wire 50 往復 pass。port_speed は CH340 のまとまった落ちで判定が揺れる）|
| `esp32-series-30eda0e31108` X035 のジグ（ESP32-P4）、`esp32-series-30eda0e343c6` 2 枚目の P4 | esp32p4 | esp32p4 | 動いている probe へ app の `.bin` を USB DFU 1.1 で（pyusb。`dfu-util -D` と同じ: DFU の interface を class FE/01 で探し、wTransferSize は functional descriptor から、DNLOAD をブロックごと、長さ 0 の DNLOAD で manifest）、device が消えて戻るのを待つ。WSL では `usbipd.exe attach --wsl --busid <busid>`（表の `usbip_busid`） | **済**（X035 治具、main の 0.0.26、2026-10-02: DFU は interface 4 / 4096 B で 8 s。RVSWD 越しの wire 50 往復 pass。uart は治具の設定の plan rx 12 / tx 6 で試す）。2 枚目の P4 は手で DFU（旧 firmware では interface 7 / 1024 B）で焼けたが、USB serial が変わり Windows の usbipd の再 bind が要る |
| `9489dd2ae0953650` SparkFun Pro Micro RP2350（1b4f:0026。unit id が鍵） | rp2 | promicrorp2350 | CDC の口へ 1200 baud のタッチ（BOOTSEL）、boot ROM の USB device（`RP2350 Boot` / `RP2 Boot`。product と serial で選ぶので、ホストのほかの RP2 には触れない）を待ち、`picotool load -x <uf2> --bus --address`（`OEP_HW_PICOTOOL`、既定は PATH の `picotool`。[picotool 2.3.1 Linux x86_64](https://github.com/raspberrypi/pico-sdk-tools/releases/download/v2.3.1-0/picotool-2.3.1-x86_64-lin.tar.gz)）、CDC の口が戻るのを 45 秒まで待つ。`OEP_HW_UF2_DRIVE=<mount>` なら代わりにその BOOTSEL のドライブへ `.uf2` を置く（ドライブをマウントできるホスト）。どちらもできなければ焼かずに、入っている firmware で試験を続ける | **済**（picotool、2026-10-02。最初の一巡は UART が使えないピンで `uart` の途中に probe が固まった → oep-probe-arduino 654b06a で修正、そのビルドでは全部 pass）|

ATOM の変換（CH552 の FTDI 互換）は probe→host をまとめて落とす（ある分は 0 %、次の分は 10〜55 %、2026-10-02）: ATOM での port_speed の失敗は 1 回やり直してから数える。port_speed の門としては CH340 の治具のほうが安定している。

`OEP_HW_NOFLASH=1` はどのボードでも焼かずに、入っている firmware を試す（"on-board" として記録。firmware の文字列は記録する
だけで照合しない）。

`rp2040` / `rp2350`（Pico）の profile も同じ rp2 の焼き方で、ボードの表に行を足せばよい。

## 共有のジグ: 使うときの決まり

V003 のジグ、2 枚の ESP32-P4 のジグ、WCH-Link はこのホストでは ArduinoCore-CH32RV の bench（別のセッションの機材）のもの。
リリースの試験で網羅できるようボードの表には載せてあるが、**ジグで回すのは bench の許可を待ってから**: 毎回まず聞く。渡されて
いない device は焼かない、リセットしない。ATOM（`/dev/ttyUSB1`）が OEP の試験に自由に使えるボード。表の `notes` に `jig` と
あるものが共有（`Board.shared`）。

## 環境変数

| 変数 | 意味 |
|---|---|
| `OEP_HW_BOARDS` | ボード id のコンマ区切り（必須。無ければ何も回らない） |
| `OEP_PROBE_DIR` / `OEP_PROBE_VERSION` / `OEP_HW_NOFLASH` | firmware の出所（どれか 1 つ） |
| `OEP_HW_TARGET`、`OEP_HW_RESET`、`OEP_HW_TARGET_ADDR`、`OEP_HW_LOOPS` | wire の試験: target が繋がっている（どこに）、reset の線、block の番地、回数 |
| `OEP_HW_GPIO`、`OEP_HW_DISABLE`、`OEP_HW_UART`、`OEP_HW_UART_LOOP` | fixture の試験のチャネルの上書き |
| `OEP_HW_RATES`、`OEP_HW_FRAMES`、`OEP_HW_LT_TIMEOUT`、`OEP_HW_ERROR_MAX` | port_speed の候補、1 条件のフレーム数、答えの待ち、判定の床（既定 5 %） |
| `OEP_HW_PICOTOOL`、`OEP_HW_UF2_DRIVE`、`OEP_HW_USBIP_BUSID`、`OEP_HW_CACHE` | ツールと環境の細部 |

ツール: `arduino-cli`（ローカルのビルド）、`esptool`（classic ESP32）、`picotool`（RP2）。DFU と boot ROM の探索は pyusb
（クライアントの依存に既にある）。`gh`、`dfu-util`、マウントしたドライブは要らない。
