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
| `host` | 要求と結果、session_id とロック、`call()`（失敗なら例外）、pipeline、エラーの階層（`OepError` / `Rejected` / `Failed`） |
| `link` | transport: シリアルの口（常に COBS + CRC、`0x00 <COBS> 0x00`、フレームの外は雑音として捨てる、排他で開く）、USB vendor bulk / HID と TCP（長さつきフレーム、§5.1 の立て直し）、corr による照合と送り直し、`open_host(target)` |
| `core` | インターフェースを名前で探す（キャッシュつき）、confirm、probe の describe（ラベル、transport の一覧）、ロックの取り方（`take`）、ピンの割り当て（plan）、`Interface` の土台 |
| `riscv` | `oep.wire.rvswd` / `oep.wire.swio`、`oep.target.riscv-dm`、リセット線の探索、GPIO 経由の attach |
| `console` | `oep.target.console`（位置つきのストリーム）と、バイト列として読む `ConsoleIO` |
| `fixture` | `oep.fixture.gpio` / `uart` / `i2c-target` / `spi-target`（revision 1） |
| `config` | `oep.probe.config`（スロット、bind、plan / label / idle の項目、get / set / save / erase、スロットと bind の今の状態） |
| `capture` | `oep.fixture.logic`（revision 1、oep-spec の oep-if-capture）。読んだ区画は `Host.on_capture` の callback に `CaptureRecord` で渡る（記録の受け口。wireskein には依存しない） |
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
oep config show <probe>                      # 設定とスロット / bind の今の状態
oep config slot <probe> --name x035 --wire rvswd --pins 2,54 --attach at-boot --retry 1 --mechanism dmseq
oep config bind <probe> --port 1 --mode last-reset --stream slot:x035
oep config save <probe>                      # 再起動の後も残す（remove / erase もある）
```

`<probe>` はシリアルの口、`tcp://HOST:PORT`、`usb[:VID:PID[:SERIAL]]`。変更はロックを取り（owner "oep config"）、終わったら
セッションを閉じる。変更はすぐ効き、`save` の後は再起動しても残る。
実機での一通りの確認は ArduinoCore-CH32 の `tests/manual/oep_smoke/`（`oep_smoke.py`、`oep_probe_checks.py`）。

## 偽の probe（動く spec）

`endpoint.Endpoint` は oep-spec の規範どおりに答える偽の probe で、ch32rv・この client・probe の firmware を突き合わせる
「動く spec」として使う（spec が変わったら、probe の firmware より先にここを合わせる）。`fake` は宣言の例（profile:
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
区画すべてに flags bit2 を立てる。出来事とデータの push は pty にも TCP（両方の framing）にも出る。ほかは `--help`。

v0 の client（`oep_client.v0`）は 2026-09-26 に消した（git の履歴に残る）。v0 を話す probe はもう無い。
