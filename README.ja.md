# OEP Python client

[English](README.md)

Open Embedded Probe の host 側。v1（oep-spec の `docs/oep-core.ja.md` と `docs/oep-if-*.ja.md`、固める候補の形）を話す。番号は oep-spec の
`generated/oep-v1/oep_v1_registry.py` をそのまま写した `oep_client.registry` から取る。破壊的変更を前提とする
実験段階で、互換 API は約束しない。OEP を初めて読む人は oep-spec の `docs/review-guide.ja.md`（どこに何が書いてあるか）から。

target の知識は host にある、という OEP の分担に従う。probe は線と DMI / DP・AP の転送しか知らず、CH32 の flash
コントローラ、RAM ローダー、RP2350 の boot ROM、Cortex-M の debug レジスタなどはここに置く。

```sh
pip install oep-client-python     # PyPI (import oep_client); a checkout: pip install -e <checkout>
uv run pytest                                                            # in a checkout
```

`import oep_client` だけで使える（`sys.path` に `src/` を足す使い方は不要になった）。番号の表は `tools/sync_registry.sh` で
oep-spec から写す。PyPI の `oep-client` は別のプロジェクトなので、配布名は `oep-client-python`。

リリースは GitHub Actions の Release（workflow_dispatch、version = X.Y.Z か X.Y.ZbN）: `tools/prepare_release.py` が
pyproject.toml、uv.lock、`oep_client.__version__` を書き換え、CHANGELOG.md の Unreleased をその版にし、試験と build の後に commit と tag、
GitHub Release、PyPI（Trusted Publishing）へ出す。変更は CHANGELOG.md の Unreleased に (EN) / (JA) で書き足しておく。

## モジュール（`oep_client`）

下の表のモジュールが公開の API です。直接 import します（`from oep_client import riscv`）。表に無いものと、`_` で始まる名前は
変わることがあります。probe の firmware とこのクライアントは版で組になります: OpenEmbeddedProbe X.Y.Z と oep-client-python
X.Y.Z（v1 の凍結までは、どのリリースも wire を壊しうるので、版を一緒に動かします）。

| モジュール | 中身 |
|---|---|
| `host` | 要求と結果、session_id とロック、`call()`（失敗なら例外）、pipeline、エラーの階層（`OepError` / `Rejected` / `Failed`。lease 切れなら `Expired`。黙って open し直さず、`Host.epoch` が進む） |
| `link` | transport: シリアルの口（常に COBS + CRC、`0x00 <COBS> 0x00`、フレームの外は雑音として捨てる、排他で開く）、USB vendor bulk / HID と TCP（長さつきフレーム、§5.1 の立て直し）、corr による照合と送り直し、`open_host(target)` |
| `core` | インターフェースを名前で探す（キャッシュつき）、confirm（probe の `boot_id` つき）、probe の describe（宣言だけ。起動の間 cache: ラベル、transport の一覧、`max_op_ms`）、ロックの取り方（`take`）、ピンの割り当て（plan）、`Interface` の土台 |
| `riscv` | `oep.wire.rvswd` / `oep.wire.swio`（scan、attach: `max_speed` は常に送る、`reset=(channel, hold_ms)` でリセットをかけながら attach、detach、connections）、`oep.target.riscv-dm`（応答は値の数を持つ。`RunResult.not_halted`）、リセット線の探索、GPIO 経由の attach |
| `console` | `oep.target.console`（位置つきのストリーム: read の応答は長さを持ち、マークは `time_ns`、ロック不要の `streams()`）と、バイト列として読む `ConsoleIO` |
| `fixture` | `oep.fixture.gpio` / `uart`（ストリームは plan が作る。`status()`）/ `i2c-target` / `spi-target`（revision 1） |
| `config` | `oep.probe.config`（スロット、bind、plan / label / idle / uart の項目、get / set / unset / save / erase。`describe()` = 宣言、`state()` = 保存・スロット・bind の今の状態、`hash_of(items)` = probe と同じ hash） |
| `capture` | `oep.fixture.logic` / `analog` / `capture-group`（revision 1、oep-spec の oep-if-capture）。start ごとに世代（`LogicCapture.generation`）が進み、read と release はそれを付ける（`read_segment(segment)` は自分で付ける）。`status()` は `Status` を返す。読んだ区画は `Host.on_capture` の callback に `CaptureRecord` で渡る（記録の受け口。wireskein には依存しない） |
| `decode` | キャプチャのチャネルの復号（I2C） |
| `registry` | oep-spec の番号の表から生成したモジュール（編集しない。oep-spec から写し直す）。名前からインターフェースの番号を引く公開の入口は `registry.INTERFACES[name]`（`.revision`、`.op`、`.tlv`、`.enum`。例 `INTERFACES["oep.fixture.uart"].enum["role"]`）。`FIXTURE_UART` などのモジュールの名前は同じもの |
| `arm` | `oep.wire.swd`、`oep.target.arm-adi`、MEM-AP、Cortex-M の停止と関数呼び出し |
| `ch32_flash` | CH32 の書き込み（RAM ローダー、ページ単位の書き直し） |
| `rp2350` | RP2350 の boot ROM 経由の flash と reboot |
| `uiapduino` | UIAPduino のブートローダへの出入り |
| `catalog` / `names` / `interfaces` / `dump` | 能力の一覧と describe の形、表示 |
| `fake` / `endpoint` / `fake_serial` / `fake_serve` | 偽の probe（下の「偽の probe」） |

## 使い方の例

```python
from oep_client import core, link, riscv, ch32_flash

hst = link.open_host("/run/board-identify/by-id/esp32-series-30eda0e31108")   # pipelining つき
# a serial port (always COBS), "tcp://127.0.0.1:PORT" (a broker), "usb:<unit id>" (the probe whose USB serial it is),
# "usb" / "usb:303a:0002[:SERIAL]" (vendor, then HID)
core.take(hst, 30000, owner="flash script")   # the only way in: force; else wait out the lease, name the holder
wire = riscv.Wire(hst, "oep.wire.rvswd")
conn, _ = wire.attach(halt=True)
dm = riscv.RiscvDm(hst, conn)
dm.reset_halt()
result = ch32_flash.program(hst, dm, open("sketch.bin", "rb").read(), ch32_flash.PROFILES["x035"])
dm.reset(confirm=True)
wire.detach(conn)
hst.end()
```

## `oep` の命令

```sh
oep dump --port <probe>                      # 能力の一覧（--fake p4-x035 でハードウェアなし）
oep config show <probe>                      # 設定、宣言、今の状態
oep config state <probe>                     # スロット / bind / 保存の今の状態だけ（ロック不要。監視用）
oep config slot <probe> --name x035 --wire rvswd --pins 2,54 --attach at-boot --retry 1 --mechanism dmseq
oep config bind <probe> --port 1 --mode last-reset --stream slot:x035
oep config uart <probe> oep.fixture.uart 115200 --format 8N1   # その UART の plan に RX / TX が付くたびに掛かる
oep config save <probe>                      # 再起動の後も残す（remove = unset、erase もある）
oep speed <probe> 1500000,921600,500000      # port_speed: UART bridge の速い速さを試し、結果を出す（下）
```

`<probe>` はシリアルの口、`tcp://HOST:PORT`、`usb[:VID:PID[:SERIAL]]`。変更はロックを取り（owner "oep config"）、終わったら
セッションを閉じる。変更はすぐ効き、`save` の後は再起動しても残る。
実機での一通りの確認は ArduinoCore-CH32 の `tests/manual/oep_smoke/`（`oep_smoke.py`、`oep_probe_checks.py`）。

## UART bridge を速くする（port_speed、使うときだけ）

describe に `port_speed` を宣言する probe（oep-core §3.5。参照の classic ESP32 の firmware は宣言する）では、host は 1 つのセッションの
間、UART bridge を起動時の速さ（115200）より速くできます。host が頼まない限り何も変わりません:

```python
hst = link.open_host("/dev/ttyUSB0", port_speed=[1500000, 921600, 500000])   # ロックを取り、セッションを開いたままにする
print(hst.link.speed.to_text())             # 試した速さごとに、実際の速さ、in / out の KB/s、壊れたフレーム、今の速さ
# 取ってあるセッションの中では: report = link.raise_speed(hst, [1500000, 921600], verify_bytes=32768, verify_s=1.0)
```

速さごとに順に: `試す`（今の速さで応答してから probe が切り替える）→ host も切り替える → 両方向に max_frame の大きさのフレームで
（link_source / link_sink、パイプライン）`verify_bytes` か `verify_s` の分だけ確かめ、壊れたフレームを数え、それぞれの向きの KB/s を
測る → 何も壊れなければ `決める`、壊れれば戻して起動時の速さに戻り、そこで confirm し直す（戻すが届かなかった probe は `verify_ms`
を待って戻る）。最初に通った速さを使う。probe の UART が作れない速さは飛ばす。通る速さは変換チップとドライバで決まる（FTDI は
3 MHz ÷ n だけ、CH340 は 921600 は通り 1500000 は probe → host が壊れた: oep-spec docs/uart-speed-negotiation.ja.md）ので、速さは
host が選ぶ。結果（`link.speed`: `rate`、`chosen`、`in_kb_s` / `out_kb_s`、`trials`）はキャプチャや書き込みの予算を立てるのに使う。

probe はセッションが終わったとき（`end`、lease の期限切れ、force）、フレームが壊れたとき、線が黙ったとき（`idle_ms`）に自分で
起動時の速さに戻る: link は `end` にはすぐ合わせ、上げた速さで応答の来ない要求（送り直しも）があれば、起動時の速さに戻って
confirm し、そこでもう一度送る（固まらない）。機能の無い probe は `not supported` で、速さは変わらない。ブローカー（TCP）の後ろでは
client ではなくブローカーが行う。シリアルの口は、ドライバにあれば low-latency のモードで開く（FTDI の latency timer 16 → 1 ms で
UART bridge の速度が 3 倍になった）。ボードの起動時の速さが 115200 でなければ `open_host(..., baud=)` で渡す。

## 偽の probe（動く spec）

`endpoint.Endpoint` は oep-spec の規範どおりに答える偽の probe で（2026-10-01: 応答のデータと並びは長さを持ち、どの応答にも TLV が
続けられる、期限切れ後の rejected `expired`、資源番号は 1 つの空間、describe は宣言だけで状態は `state`、キャプチャの世代）、
ch32rv・この client・probe の firmware を突き合わせる「動く spec」として使う（spec が変わったら、probe の firmware より先にここを合わせる）。`fake` は宣言の例（profile:
`p4-x035`、`esp32-v003`、`p4-bench` = スロット 3 か所と席 2 つの架空の治具、`rp2350-pins` = host がピンを選ぶ wire）、`fake_serial` は
シリアルの口のバイトの側（COBS の候補、生のバイトと bind、セッション中の停止と再開）。

`fake_capture` は `oep.fixture.logic`（ロジック）: ワンショット、リピート（実際のレートで時計どおりに区画ができ、リング、release）、
ストリーミング（購読している間のデータの push）、レベル / エッジのトリガとプリトリガ、出来事。取れるものは決まっている: サンプル i は
カウンタの値 i で、チャネル k はそのビット k（周期 2^(k+1) サンプルの方形波）。layout は profile が許す形（`p4-x035`: P4 の
PARLIO と同じ w 1〜16、3 本なら w 4。`esp32-v003`: classic ESP32 の sampler と同じ w 8、ワンショットだけ）。capture は聞くだけ
なので、ほかのインターフェースが持つピンにも plan できる。`p4-x035` には `oep.fixture.analog`（P4 の ADC1 の 4 チャネル、GPIO16〜23。
偶数のチャネル k は方形波、奇数は正弦波で、周期は 64 (k // 2 + 1) サンプル。16 ビットの枠に 12 ビットの値。ESP32 と同じ形の
frontend、作り物の 2 点の較正と Vrefint）と、ロジックとアナログを束ねる `oep.fixture.capture-group` もある（一緒に始まり、
アナログは 5 µs 遅れ（±2 µs）、片方のトリガが両方に印される）。時刻は probe の 1 本の時計の ns。

外のプログラムの試験には `fake_serve` を子プロセスで使う:

```sh
uv run python -m oep_client.fake_serve --pty --profile p4-bench --slot x035 --bind last-reset \
    --console 'uptime %d\r\n' --every 100
# 最初の行: PTY /dev/pts/N（--tcp 0 なら PORT n）。stdin を閉じると終わる
```

pty がシリアルの口（host が TIOCEXCL を掛けて開く）、`--tcp PORT` は `--framing cobs`（シリアルの口）か `--framing length`
（vendor bulk / TCP の形）。故障の注入は `--drop N`（N 番目の答えを 1 回出さない。要求は実行済みなので送り直しは覚えた答えを
受ける）、`--noise TEXT`（答えの前に雑音）、`--corrupt N`（N 番目の答えの CRC を 1 回壊す）。`--capture-slipped` は capture の
区画すべてに flags bit2 を立てる。port_speed: `esp32-v003` は持つ（`--no-port-speed` で外す）。
`--broken-rate RATE[:MIN_SIZE][:in|out]` はその速さでフレームを壊す（同じプロセスの `fake_serial.FakeSerialStream` は、host の速さが
probe の速さと違う間、両方向のバイトをすべて壊す）。出来事とデータの push は pty にも TCP（両方の framing）にも出る。ほかは `--help`。

v0 の client（`oep_client.v0`）は 2026-09-26 に消した（git の履歴に残る）。v0 を話す probe はもう無い。
