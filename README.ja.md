# OEP Python client

[English](README.md)

Open Embedded Probe の host 側。v1（oep-spec の `docs/oep-core.ja.md` と `docs/oep-if-*.ja.md`、固める候補の形）を話す。番号は oep-spec の
`generated/oep-v1/oep_v1_registry.py` をそのまま写した `oep_client.registry` から取る。破壊的変更を前提とする
実験段階で、互換 API は約束しない。OEP を初めて読む人は oep-spec の `docs/review-guide.ja.md`（どこに何が書いてあるか）から。

target の知識は host にある、という OEP の分担に従う。probe は線と DMI / DP・AP の転送しか知らず、CH32 の flash
コントローラ、RAM ローダー、RP2350 の boot ROM、Cortex-M の debug レジスタなどはここに置く。

```sh
pip install oep-client-python     # PyPI (import oep_client); a checkout: pip install -e <checkout>
uv run pytest                                                            # in a checkout（偽の probe。実機なし）
OEP_HW_BOARDS=<board id> OEP_PROBE_DIR=<oep-probe-arduino の checkout> uv run pytest tests/hw -m hw   # 実機: tests/hw/README.ja.md
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
| `riscv` | `oep.wire.rvswd` / `oep.wire.swio`（scan、attach: `max_speed` は常に送る、`reset=(channel, hold_ms)` でリセットをかけながら attach、detach、connections）、`oep.target.riscv-dm`（応答は値の数を持つ。`RunResult.not_halted`）、リセット線の探索（`find_reset_line(candidates, pins=...)`）、GPIO 経由の attach |
| `targets` | host が target の系統ごとに知っていることを 1 つの表に（`FAMILIES`: 線、target_id の照合、リセットのベクタ、NRST を option で読む関数、max_speed / idle_clock）。`identify(target_id)` |
| `pins` | `oep pins`: channel の分類、low に保つ探索、scan、識別、リセットの線の確かめ、スロットの提案（`PinFinder`） |
| `console` | `oep.target.console`（位置つきのストリーム: read の応答は長さを持ち、マークは `time_ns`、ロック不要の `streams()`）と、バイト列として読む `ConsoleIO` |
| `fixture` | `oep.fixture.gpio`（出力の強さを要素ごとに: `set([(ch, mode, Drive.max_ma(10))])`。`drive_levels()`、`read_state()` = level といま効いている段）/ `uart`（ストリームは plan が作る。`status()`）/ `i2c-target` / `spi-target`（revision 1） |
| `config` | `oep.probe.config`（スロット - `boot_reset` -、bind、plan / label / idle - その `drive` - / uart の項目、get / set / unset / save / erase。`describe()` = 宣言、`state()` = 保存・スロット・bind の今の状態（`reset_at_ns` も）、`hash_of(items)` = probe と同じ hash、`find_line(cfg, slot, "nrst")` = probe.config §1.3 の線の探し方） |
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
# a serial port (always COBS), "tcp://127.0.0.1:PORT" (a broker), "usb:<unit id>" (the device whose USB serial it is;
# describe's unit_id must match), "usb" / "usb:303a:0002[:SERIAL]" (vendor, then HID). Each is probed first with a
# confirm only (core §3.3): no valid answer -> closed, link.NotOepProbe
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
oep speed <probe> [--candidates 921600,500000] [--verify [--flows out:2]]  # port_speed: 速い速さを試し、結果を出す（下）
oep pins <probe> --power 5 --wire swio       # target のつながり方: debug のピン、リセットの線、スロット（下）
```

`<probe>` はシリアルの口、`tcp://HOST:PORT`、`usb[:VID:PID[:SERIAL]]`。変更はロックを取り（owner "oep config"）、終わったら
セッションを閉じる。変更はすぐ効き、`save` の後は再起動しても残る。
実機での一通りの確認は ArduinoCore-CH32 の `tests/manual/oep_smoke/`（`oep_smoke.py`、`oep_probe_checks.py`）。

## ピンを探す（`oep pins`）

`oep pins <probe> [--power CH] [--exclude CH,...] [--wire swio|rvswd|swd] [--steps ...] [--save] [--json]` は、ピンを host
が選ぶ probe で、target がどこにつながっているかを探します。各段は何をしたかを出し、全体は 1 分以内に終わり、最後に plan を
すべて解きます。手順とその理由は oep-spec の host 開発ガイド（「ピンの探し方（参考）」）にあります。target の系統ごとに要る
こと（線、リセットのベクタ、リセットの線の有無を読む option の読み方、max_speed / idle_clock）は 1 つの表 `targets.FAMILIES`
にまとめ、その出典は oep-spec の `docs/target-scan-notes.ja.md` に記録します。

1. **classify**: `oep.fixture.gpio` が許すすべての channel を、pull-up、両方の pull、pull-down の順に 16 回ずつ読む: floating、
   pulled-up（両方の pull でも 1: リセットの線のような弱い pull-up）、driven-high / driven-low（どちらの pull でも同じレベル:
   push-pull か強い pull。idle-high の UART の線はこう見える）、active（読むたびに変わる）。`--power CH` では先に target の
   電源を切って読み、違って読めた channel が電源に従う。どれも候補を出すだけ。
2. **hold**: floating / pulled-up の channel を 1 本ずつオープンドレインで low に保ち、active の channel を見る。止まれば
   リセットの線の候補。
3. **scan**: floating / pulled-up の channel で線を scan する（rvswd / swd は組で、600 組まで）。driven / active の channel、
   電源の channel、`--exclude` は scan しない。
4. **identify**: 答えた線に attach（halt）し、target_id で系統を見る。CH32V00x は option bytes で NRST の有無を見る（読むだけ、
   書かない）。その後 resume。
5. **reset**: 候補ごとにリセットをかけながら attach する。本物の線なら hart はリセットのベクタで止まる。
6. **slot**: `oep config slot` の行と、label `<スロット>.nrst` / `<スロット>.power_hi` を勧める。label は probe の設定に対して
   `config.find_line` で確かめる（別の channel がもうその名前を持っていれば、線が見つからなくなるので note で言う）。
   `--save` で書く（set + save）。`--save` が無ければ何も書かない。

安全: driven / active の channel は駆動も scan も保持もしない。電源の channel は `--power` のときだけ触る。low に保つのは
オープンドレインだけ。gpio の plan を新しくすると、probe は前の plan のピンを（電源の channel も）いったん離すので、`--power`
では新しい plan のたびにきれいに電源を入れ直す（報告の `power_cycles`）。

ESP32-P4 と CH32V003（電源は GPIO5）、2026-10-02:

```
$ oep pins /run/board-identify/by-id/esp32-series-30eda0ea068b --power 5 --wire swio
power: channel 5 low 300 ms (target off: read), then high 400 ms before the reads
classify: 52 channels, 16 reads each under pull-up, both pulls, pull-down
  floating     0-3,6,9-20,26-34,36-50,52-54
  pulled-up    4
  driven-high  7-8,22-23,35  (never scanned or held)
  driven-low   51  (never scanned or held)
  active       21  (21: 8 changes under pull-up)
  follow power 4,6,9-11,13,15-16,19-23,32-33  (read otherwise with the target off: wired to it; candidates only)
reset line (hold low): 45 candidates, each held low (open drain) up to 390 ms while watching 21 (105 changes in 1.0 s running, longest lull 130 ms)
  hold 4 low: 21 stopped
  45 held in 3.4 s: stopped by 4
scan swio: 45 channels (0-4,6,9-20,26-34,36-50,52-54; not 7-8,21-23,35,51: driven / active) in 0.14 s -> 19
attach swio 19: target_id 00310510 (WCH DMI 0x7F) -> ch32v00x, halted at dpc 0x108
  option bytes (read only): RST_MODE 10 (USER 0xf7): NRST on PD7, 12 ms ignore window
  attach under reset through 4 (held 20 ms): dpc 0x0 -> the reset line
slot: oep config slot ... --name ch32v00x --wire swio --pins 19   (not written; --save writes it)
label: oep config label ... 4 ch32v00x.nrst   (not written)
label: oep config label ... 5 ch32v00x.power_hi   (not written)
idle:  oep config idle ... 5 output-high   (keeps the target powered while no plan holds channel 5; not written by this tool)
released every plan (channel 5 is back to its idle state: the target is powered only while something drives it)
done in 7.3 s
```

19 が SWIO、4 が NRST（hold で 21 が止まり、リセットをかけながらの attach で dpc 0）。22 / 23 は target の UART（idle high
なので driven として scan も保持もしない）、21 はアプリが動かす出力。

## UART bridge を速くする（port_speed、使うときだけ）

describe に `port_speed` を宣言する probe（oep-core §3.5。参照の classic ESP32 の firmware は宣言する）では、host は 1 つのセッションの
間、UART bridge を起動時の速さ（115200）より速くできます。host が頼まない限り何も変わりません:

```python
hst = link.open_host("/dev/ttyUSB0", port_speed=[921600, 500000])    # ロックを取り、セッションを開いたままにする
print(hst.link.speed.to_text())             # 候補ごとに、基準、流し方、今の速さ
# 完全な形（基準と流し方を測る）を取ってあるセッションの中で:
report = link.raise_speed(hst, [921600, 500000], verify=True, flows=[("out", 2)], record=True)
```

手順は oep-spec の host 開発ガイド §7（core §3.5 は握手だけ）。**最小の形**（既定、約 50 ms、計測なし）: 候補ごとに順に
`試す`（今の速さで応答してから probe が切り替える）→ host は要求した baud に切り替える → 20 ms → `confirm`（100 ms、3 回まで）→
`決める`。**完全な形**（`verify=True` か `flows=` を渡す）: 起動時の速さの基準を流し方ごとに取り（このセッションのフレーム、
無ければ 60 フレーム）、候補ごとに使う流し方だけ流す。流し方 = `("in"|"out"|"duplex", n)`（in = link_source probe → host、
out = link_sink host → probe、duplex = 両方を交互。`n` は同時数、0 = link が出す最大）で、max_frame − 16 のフレームを 16 個流し、
壊れと失われを数え KB/s を測る。壊れ + 失われが 3 以上で割合が max(基準 × 2, 5 %) を超えたら流し方は通らず、n = 1 で流し直し
（通れば n = 1 が link の上限）、1 つでも通らなければ候補は通らない。通らない候補は戻して（step 2）起動時の速さに戻り confirm し直す。
最初に通った候補を使う。probe の UART が作れない速さは飛ばす。通る速さは変換チップとドライバで決まる（FTDI は 3 MHz ÷ n だけ、
CH340 は 921600 は通り 1500000 は probe → host が壊れた: oep-spec docs/uart-speed-negotiation.ja.md）ので、速さは host が選ぶ。
結果（`link.speed`: `base`、`rate`、`chosen`、`baseline`、`trials` = `flows` と `in_kb_s` / `out_kb_s` / `duplex_kb_s` を持つ
`SpeedTrial`）はキャプチャや書き込みの予算を立てるのに使う。

probe はセッションが終わったとき（`end`、lease の期限切れ、force）、フレームが壊れたとき、線が黙ったとき（`idle_ms`、最長
`port_speed_idle_max_ms` = 3 秒。落ちた host の速さもそれより長くは残らない）に自分で起動時の速さに戻る: 上げている間、link は
`idle_ms` の半分より短く黙れば要求の前に keepalive を送り、長く黙る呼び出し側は `hst.link.keep_alive()` で同じことをする。
シリアルの口を開くと最初の confirm を約 4 秒繰り返して、残った速さが戻るのを待つ。link は `end` と戻すにはすぐ合わせ、上げた速さで
応答の来ない要求（送り直しも）があれば起動時の速さに戻って confirm し、そこでもう一度送る（固まらない）。上げている間は応答を待つ
1 回が lease の 4 分の 1 までなので、lease の内に十分収まる。使っている間、決めた速さの最初の 32 KiB と 1 秒（`probation_bytes`、
`probation_s`）は試用期間で、そこで壊れ・失われが 3 以上かつ max(基準 × 2, 5 %) 超、または応答が来なければ、確かめの失敗として
すぐ下げる（16 フレームの確かめは素早い関門として残す）。その後は直近 3 秒のフレーム（50 未満なら判定しない）を見て、
max(基準 × 2, 10 %) を超えて壊れ・失われたら下げる。下げるのは、上げた速さで port_speed の戻すを送り、起動時の速さに戻って confirm
し、その呼び出しの候補のうちこのセッションで通らなかったものより下の次の候補を新しく試す → confirm → 確かめ → 決める（残って
いなければ起動時の速さ）。壊れた速さとそれより上はそのセッションの間は使わない（`speed.stepped_down`、`speed.down_why`、
`to` と `probation` を持つ `speed.step_downs`）。`max_tries=` は 1 回の呼び出しで試す候補の数の上限（キャプチャの host は 2）。
`record=True`（真偽値・パス・`speed_record.SpeedRecord`。`oep speed` CLI は既定で ON、ライブラリは OFF）は、通った / 通らなかった
速さを（口、unit_id）ごとに `~/.cache/oep-client/link-speed.json` に残し（通ったは 30 日、通らなかったは 1 日、別の速さの破綻から
2 秒（`settle_s`）以内に測った失敗は「不明」）、通った速さを先頭に、通らなかった速さを外す。全部の候補が通らなかったとあれば、
いちばん遅い候補を 1 回だけ試す（`speed.retried`）。機能の無い probe は `not supported`
で、速さは変わらない。ブローカー（TCP）の後ろでは client ではなくブローカーが行う。シリアルの口は、ドライバにあれば low-latency の
モードで開く（FTDI の latency timer 16 → 1 ms で UART bridge の速度が 3 倍になった）。ボードの起動時の速さが 115200 でなければ
`open_host(..., baud=)` で渡す。

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

`oep.fixture.i2c-target` と `oep.fixture.spi-target`（`p4-x035`、`esp32-v003`）は plan で役割を受け（SDA / SCL、SCK / MOSI / MISO / CS。
`esp32-v003` の SPI は 2 つの channel_group のどれかに完全に一致）、fixture §3 / §4 の op にすべて答える。バスの controller は無い:
試験が endpoint の hook（`i2c_write` / `i2c_read` / `spi_transfer`: バス上の 1 回のトランザクション）を呼ぶまで、arm は待ったままで何も受けない。

外のプログラムの試験には `fake_serve` を子プロセスで使う:

```sh
uv run python -m oep_client.fake_serve --pty --profile p4-bench --slot x035 --bind last-reset \
    --console 'uptime %d\r\n' --every 100
# 最初の行: PTY /dev/pts/N（--tcp 0 なら PORT n）。stdin を閉じると終わる
```

pty がシリアルの口（host が TIOCEXCL を掛けて開く）、`--tcp PORT` は `--framing cobs`（シリアルの口）か `--framing length`
（vendor bulk / TCP の形）。故障の注入は `--drop N`（N 番目の答えを 1 回出さない。要求は実行済みなので送り直しは覚えた答えを
受ける）、`--noise TEXT`（答えの前に雑音）、`--corrupt N`（N 番目の答えの CRC を 1 回壊す）。`--capture-slipped` は capture の
区画すべてに flags bit2 を立てる。`--no-drive-levels` は gpio の drive_levels を外す（出力の強さを切り替えられない probe）。
`--silent-until-reset N` は N 番目のピンの組の target を、その線でリセットされるまで何も答えないようにする。`--boot-reset`（どの
`--slot` も起動直後のリセットでのやり直しを求める）と `--label CH=TEXT`（例 `23=v003.nrst`）と合わせると、起動時にリセットでのやり直しが起きる。port_speed: `esp32-v003` は持つ（`--no-port-speed` で外す）。
`--broken-rate RATE[:MIN_SIZE][:in|out]` はその速さでフレームを壊す（同じプロセスの `fake_serial.FakeSerialStream` は、host の速さが
probe の速さと違う間、両方向のバイトをすべて壊す）。出来事とデータの push は pty にも TCP（両方の framing）にも出る。ほかは `--help`。

v0 の client（`oep_client.v0`）は 2026-09-26 に消した（git の履歴に残る）。v0 を話す probe はもう無い。
