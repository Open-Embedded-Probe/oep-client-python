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
pyproject.toml と `oep_client.__version__` を書き換え、CHANGELOG.md の Unreleased をその版にし、試験と build の後に commit と tag、
GitHub Release、PyPI（Trusted Publishing）へ出す。変更は CHANGELOG.md の Unreleased に (EN) / (JA) で書き足しておく。

## モジュール（`oep_client`）

| モジュール | 中身 |
|---|---|
| `host` | 要求と結果、session_id とロック、`call()`（失敗なら例外）、pipeline、エラーの階層（`OepError` / `Rejected` / `Failed`） |
| `link` | transport: シリアルの口（常に COBS + CRC、`0x00 <COBS> 0x00`、フレームの外は雑音として捨てる、排他で開く）、USB vendor bulk / HID と TCP（長さつきフレーム、§5.1 の立て直し）、corr による照合と送り直し、`open_host(target)` |
| `core` | インターフェースを名前で探す（キャッシュつき）、confirm、probe の describe（ラベル、transport の一覧）、ロックの取り方（`take`）、ピンの割り当て（plan）、`Interface` の土台 |
| `riscv` | `oep.wire.rvswd` / `oep.wire.swio`、`oep.target.riscv-dm`、リセット線の探索、GPIO 経由の attach |
| `console` | `oep.target.console`（位置つきのストリーム）と、バイト列として読む `ConsoleIO` |
| `fixture` | `oep.fixture.gpio` / `uart`（revision 1） |
| `config` | `oep.probe.config`（スロット、bind、plan / label / idle の項目、get / set / save / erase、スロットと bind の今の状態） |
| `capture` | `oep.fixture.capture`（revision 1、oep-spec の oep-if-capture）。読んだ区画は `Host.on_capture` の callback に `CaptureRecord` で渡る（記録の受け口。wireskein には依存しない） |
| `esp32_targets` | 独自インターフェース `io.github.ch32-riscv-ug.esp32.i2c-target` / `spi-target`（oep-probe-arduino の ESP32 の I2C / SPI の target） |
| `decode` | キャプチャのチャネルの復号（I2C） |
| `registry` | oep-spec の番号の表から生成したモジュール（編集しない。oep-spec から写し直す） |
| `arm` | `oep.wire.swd`、`oep.target.arm-adi`、MEM-AP、Cortex-M の停止と関数呼び出し |
| `ch32_flash` | CH32 の書き込み（RAM ローダー、ページ単位の書き直し） |
| `rp2350` | RP2350 の boot ROM 経由の flash と reboot |
| `uiapduino` | UIAPduino のブートローダへの出入り |
| `catalog` / `names` / `interfaces` / `dump` | 能力の一覧と describe の形、表示 |
| `fake` / `endpoint` / `fake_serial` / `fake_serve` | 偽の probe（下の「偽の probe」） |
| `target` | 上の主なものを 1 か所から import する入口（最初の版に合わせて書いた呼び出し側のため） |

## 使い方の例

```python
from oep_client import core, link, riscv, ch32_flash

hst = link.open_host("/run/board-identify/by-id/esp32-series-30eda0e31108")   # pipelining つき
# a serial port (always COBS), "tcp://127.0.0.1:PORT" (a broker), "usb" / "usb:303a:0002[:SERIAL]" (vendor, then HID)
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
`p4-x035`、`esp32-v003`、`p4-bench` = スロット 3 か所と席 2 つの架空の治具）、`fake_serial` はシリアルの口のバイトの側（COBS の
候補、生のバイトと bind、セッション中の停止と再開）。

外のプログラムの試験には `fake_serve` を子プロセスで使う:

```sh
uv run python -m oep_client.fake_serve --pty --profile p4-bench --slot x035 --bind last-reset \
    --console 'uptime %d\r\n' --every 100
# 最初の行: PTY /dev/pts/N（--tcp 0 なら PORT n）。stdin を閉じると終わる
```

pty がシリアルの口（host が TIOCEXCL を掛けて開く）、`--tcp PORT` は `--framing cobs`（シリアルの口）か `--framing length`
（vendor bulk / TCP の形）。故障の注入は `--drop N`（N 番目の答えを 1 回出さない。要求は実行済みなので送り直しは覚えた答えを
受ける）、`--noise TEXT`（答えの前に雑音）、`--corrupt N`（N 番目の答えの CRC を 1 回壊す）。ほかは `--help`。

v0 の client（`oep_client.v0`）は 2026-09-26 に消した（git の履歴に残る）。v0 を話す probe はもう無い。
