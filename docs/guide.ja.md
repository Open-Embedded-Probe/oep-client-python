# 使い方ガイド

[English](guide.md)

Python から OEP のプローブを使う手順です。プローブにつなぎ、ピンを割り当て、フィクスチャで target を動かし、波形を取り込み、
target を書いてデバッグします。例はどれも、ハードウェアの無い仮想ベンチ（[仮想ベンチ](virtual-bench.ja.md)）でも動きます。
モジュールごとの詳しい中身は [モジュール](reference.ja.md)、`oep` の命令は [`oep` の命令](cli.ja.md) にあります。

## インストール

```sh
pip install oep-client-python        # import するときの名前は oep_client
```

PyPI の `oep-client` は別のプロジェクトです。USB の vendor bulk が使えない環境では、HID か CDC の口で同じ機能が使えます。

### Linux での USB の権限（udev）

プローブはプロジェクトの USB の VID:PID `1209:4F45`（transports §3）で列挙されます。その vendor bulk（libusb）や HID（hidraw）を
普通の利用者で開くには udev の規則が要ります。[`udev/70-oep-probe.rules`](../udev/70-oep-probe.rules)（checkout と sdist にあります）は、
ログインしている利用者（`uaccess`）と `plugdev` グループに USB device とその hidraw を開かせます。入れるには管理者の権限が要ります。

```sh
sudo install -m 0644 udev/70-oep-probe.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger   # または probe を抜き差しする
```

プローブの CDC の口（`/dev/ttyACM*`）は、いつもの `dialout` グループのほかに規則は要りません。Windows と macOS では規則は要りません。

## プローブにつなぐ

```python
from oep_client import link, core

hst = link.open_host("oep://30eda0ea068b")   # プローブの unit id。USB を探し、無ければネットワーク（DNS-SD）を探す
try:
    core.take(hst, 30_000, owner="my-test")  # セッションとロック（lease 30 秒。使っている間は自動で延びる）
    print(core.unit_id(hst), core.max_op_ms(hst))
finally:
    hst.end()                                # ロックと、割り当てたピンを返す
    hst.link.close()
```

`open_host` に渡せる行き先:

| 書き方 | 意味 |
|---|---|
| `oep://<unit id>` | そのプローブ。USB を探し、無ければネットワーク。開いた後、describe の unit id が名指したものと同じことを確かめる |
| `oep://<unit id>?via=usb` / `bulk` / `hid` / `cdc` / `net` | 経路を決める |
| `oep://<unit id>?via=tty:/dev/ttyUSB0` | USB-UART の変換チップの先のプローブ（classic ESP32 など） |
| `/dev/ttyACM0`、`COM3` | シリアルの口（unit id を確かめない） |
| `tcp://HOST:PORT` | TCP のプローブ、または broker |
| `usb` | プロジェクトの VID:PID のプローブが 1 台だけのとき、それ |

ロックを持つのは 1 つのセッションだけです。ほかの host が持っているとき、`core.take` は lease が切れるまで待ち、待ちきれなければ
持ち主の名前（`owner`）を添えて断られます。

## プローブを探す

```sh
oep find                         # ネットワークのプローブ（DNS-SD の _oep._tcp。unit id、アドレス、port）
oep dump --port oep://<unit id>  # そのプローブの機能、ピン、設定の一覧
```

USB のプローブは `oep://<unit id>` でそのまま開けます（unit id は USB の serial number と同じ）。Python からは
`discovery.browse(timeout=2.0)` がネットワークのプローブを並べ、`discovery.find_unit(unit_id)` が 1 台を探します。

## ピンを名前で引き、割り当てる

プローブのピンは channel の番号で扱います。番号は起動ごとに決まるので、スクリプトには名前で書き、`core.channels_of` で番号に直します。
名前は、プローブの channel の名前（`GPIO10`）か、設定で付けた線のラベル（`x035.swdio`、`x035@pb9`。[`oep` の命令](cli.ja.md)）です。

フィクスチャは、使う前に `core.plan_apply` でピンを割り当てます。割り当ては `(fn, role, channel)` の並びで、各クラスの
`assignments(...)` が作ります。ほかの interface が持っているピンは断られます（`core.PinsTaken` は持ち主を言います）。
割り当てはセッションのもので、`core.plan_release` か、セッションの終わりで返ります。

```python
from oep_client import fixture

a, b = core.channels_of(hst, ["GPIO10", "GPIO11"])
gpio = fixture.Gpio(hst)
core.plan_apply(hst, [(gpio.fn, 1, a), (gpio.fn, 1, b)])
gpio.set([(a, gpio.OUTPUT_HIGH), (b, gpio.INPUT_PULLDOWN)])   # 1 つの要求で順に当てる
print(gpio.read([a, b]))                                      # [1, 0]
core.plan_release(hst)
```

target のリセットの線は、open-drain のモード（`gpio.OPEN_DRAIN_LOW` / `OPEN_DRAIN_RELEASE`、`gpio.pulse_low(ch)`）で動かします。
open-drain のモードは線を high に駆動しません。

## フィクスチャで target を動かす

フィクスチャは、target の周辺を試すための相手です。どれもピンを割り当ててから使います。

| クラス | すること |
|---|---|
| `fixture.Gpio` | 線を出力・入力にし、level を読む |
| `fixture.FixtureUart` / `FixtureUartIO` | target の UART の相手（`configure(baud)`、読み書きのストリーム） |
| `fixture.I2cController` / `SpiController` | プローブが controller になり、target の I2C / SPI の target を試す |
| `fixture.I2cTarget` / `SpiTarget` | プローブが target になり、target の I2C / SPI の controller を試す |
| `fixture.Pwm` / `Counter` | 方形波を出す、エッジを数えて周波数を測る |
| `fixture.Dac` | 電圧を出す |

```python
# PWM で 1 kHz を出し、同じピンをカウンタで数える
pwm, cnt = fixture.Pwm(hst), fixture.Counter(hst)
(out,) = core.channels_of(hst, ["GPIO12"])
core.plan_apply(hst, pwm.assignments(out) + cnt.assignments(out))
pwm.start(0, 1000)                    # 出力 0 に 1000 Hz、デューティ 50 %。プローブが出せる値で答える
print(cnt.measure(100_000).hz(0))     # 0.1 秒数えて -> 1000.0
core.plan_release(hst)

# I2C controller で、アドレス 0x50 に 1 バイト書いてから 4 バイト読む
i2c = fixture.I2cController(hst)
sda, scl = core.channels_of(hst, ["GPIO20", "GPIO21"])
core.plan_apply(hst, i2c.assignments(sda, scl))
i2c.configure(100_000, 10_000)        # SCL 100 kHz、ストレッチの待ちの上限 10 ms -> 使う SCL
t = i2c.transfer([fixture.I2cSegment.write(0x50, b"\x00"), fixture.I2cSegment.reading(0x50, 4)])
print(t.ok, t.data)
core.plan_release(hst)
```

速さや長さの上限は、プローブが describe で宣言します（`i2c.max_clock_hz`、`i2c.max_length` など）。範囲の外を頼むと
`host.Unsupported` で断られます。

## キャプチャ

`acquire` が、ロジックとアナログの取り込みを 1 回でまとめます。取る線と速さを `acquire.Request` に書き、プローブが実際に使った値と
データを `Result` で受け取ります。

```python
from oep_client import acquire

req = acquire.Request([("A", "GPIO10"), ("B", "GPIO11")], rate_hz=20_000_000, samples=2000,
                      trigger=("B", "rise"), pretrigger=100)
res = acquire.capture_at("oep://30eda0ea068b", req)   # プローブを開いて取り、閉じる
print(res.tick_hz, res.meta["trigger_tick"], res.complete)
for ch in res.channels:
    print(ch.name, ch.n)                               # ロジックの線は Bits（ビットを詰めたデータ）
```

- `capture_at(address, req)` はプローブを開いて閉じます。`capture(hst, req)` は、呼び手がつないだセッションで取ります。
- `mode="stream"` は流し続けます（`duration_s` 秒、または `during=` の関数が返るまで）。`mode="repeat"` はトリガのたびに 1 つずつ取ります。
- アナログの線は `analog=[("V", "GPIO16", None)]` と `analog_rate_hz` で足します。ロジックとアナログを同時に始めるときは、プローブの
  capture-group を使います。
- 失ったデータは `res.lost` に出ます。流し続けで失うと、既定ではそこで止まります（`on_loss="continue"` で最後まで取る）。
- 長い流し続けは `spool_dir=` でディスクに書きながら取ります。
- USB の vendor の口は、host が受け取りに遅れた分を 64 MiB（40 MB/s で 1.6 秒分）まで持っておきます。ディスクの書き込みが数秒止まる
  host など、もっと余裕が要るときは環境変数 `OEP_USB_BACKLOG_MB`（MiB）で増やします（例: `OEP_USB_BACKLOG_MB=512`）。持つ分は
  メモリを使います。

取り込みの速さ・チャネル数の上限はプローブごとに違い、`oep dump` が describe の値を示します。

## デバッグと書き込み

target の知識は host にあります。プローブは線と DMI / DP・AP の転送だけを受け持ち、flash の書き方などはこのクライアントが持ちます。

```python
from oep_client import riscv, ch32_flash

wire = riscv.Wire(hst, "oep.wire.rvswd")       # SWIO の石は "oep.wire.swio"
conn, _ = wire.attach(halt=True)               # 止めてつなぐ
dm = riscv.RiscvDm(hst, conn)
print(hex(dm.read_register(0x7b1)))            # dpc
print(dm.read_block(0x20000000, 4).hex())      # 4 ワード読む
dm.reset_halt()
ch32_flash.program(hst, dm, open("sketch.bin", "rb").read(), ch32_flash.PROFILES["x035"])
dm.reset()                                     # 走らせる
wire.detach(conn)
```

- target の系統ごとの線・リセット・速さは `targets.FAMILIES` にあります。どの石が何の線を使うかは
  [oep-probe-arduino のデバッグの相手](https://github.com/Open-Embedded-Probe/oep-probe-arduino/blob/main/docs/guide/debug-targets.ja.md) にあります。
- Arm（SWD）の石は `arm` を使います。RP2040 / RP2350 のボードを Arduino IDE と gdb から書いてデバッグする手順は
  [SWD のボードを書く・デバッグする](swd.ja.md) にあります。
- target のコンソール（デバッグの線越しの入出力）は `console` です。

## 設定

プローブの設定（ピンの名前やラベル、使わないピン、ネットワーク）は、`oep settings` か `settings.Settings` で読み書きします。

```sh
oep settings show oep://<unit id>
oep settings set oep://<unit id> core channel.GPIO23.label=v003.nrst --save
```

```python
from oep_client import settings
s = settings.Settings(hst)
for category, name, value in s.show():
    print(category, name, value)
```

設定のファイルへの書き出しと読み込みは `export()` / `plan_import(doc)` / `apply_import(plan)` です（oep-spec の設定ファイル）。

## メモ

host がプローブに名前つきのデータを置けます（配線の記録など）。プローブ自身は中身を読みません。

```python
from oep_client import memo
mm = memo.Memo(hst)
mm.write("my-bench/port/x035", b"...")
print(mm.read("my-bench/port/x035"), mm.list("my-bench/"))
```

## スケッチが足した interface を呼ぶ

自分のスケッチに足した interface（`oep::SimpleInterface` など）は、`call` で名前と欄から呼べます。

```python
from oep_client.call import call
r = call(hst, "io.github.yourname.mine", "add", a=2, b=3)
print(r["sum"])
```

op の名前と欄は、スケッチが describe で宣言したものです。

## プローブのログ

スケッチが `OepProbeLog.h` で書いた行は、`probe_log` がプローブの時計つきで読みます。同じ時計で取ったキャプチャのどのサンプルに
当たるかは `sample_of` で分かります。

## 試験から使う

- pytest-embedded-arduino-cli から使う: [pytest のプラグイン](pytest.ja.md)
- 実機の結合試験: [tests/hw](../tests/hw/README.ja.md)
- OEP を使うプロジェクトの試験の組み方: [利用プロジェクト向けガイド](testing-consumers.ja.md)
