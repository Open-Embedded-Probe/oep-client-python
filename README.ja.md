# OEP Python client

Open Embedded Probe の host 側。v1（oep-spec `docs/v1-core-wire-delta.ja.md`、固める候補の形）を話す。番号は oep-spec の
`generated/oep-v1/oep_v1_registry.py` をそのまま写した `oep_client.v1.registry` から取る。破壊的変更を前提とする
実験段階で、互換 API は約束しない。

target の知識は host にある、という OEP の分担に従う。probe は線と DMI / DP・AP の転送しか知らず、CH32 の flash
コントローラ、RAM ローダー、RP2350 の boot ROM、Cortex-M の debug レジスタなどはここに置く。

```sh
uv run pytest
```

## モジュール（`oep_client.v1`）

| モジュール | 中身 |
|---|---|
| `host` | 要求と結果、session_id とロック、`call()`（失敗なら例外）、pipeline、エラーの階層（`OepError` / `Rejected` / `Failed`） |
| `link` | シリアルと USB vendor bulk の transport（長さつきフレーム、USB-UART は COBS + CRC）、corr による照合、§1 の立て直し（resync）、`open_host()` |
| `core` | インターフェースを名前で探す（キャッシュつき）、confirm、probe のラベル、ピンの割り当て（plan）、`Interface` の土台 |
| `riscv` | `oep.wire.rvswd` / `oep.wire.swio`、`oep.target.riscv-dm`、リセット線の探索、GPIO 経由の attach |
| `console` | `oep.target.console`（位置つきのストリーム）と、バイト列として読む `ConsoleIO` |
| `fixture` | `oep.fixture.gpio` / `uart`（revision 1） |
| `capture` | `oep.fixture.capture`（revision 1、logic-capture の基本の形） |
| `registry` | oep-spec の番号の表から生成したモジュール（編集しない。oep-spec から写し直す） |
| `arm` | `oep.wire.swd`、`oep.target.arm-adi`、MEM-AP、Cortex-M の停止と関数呼び出し |
| `ch32_flash` | CH32 の書き込み（RAM ローダー、ページ単位の書き直し） |
| `rp2350` | RP2350 の boot ROM 経由の flash と reboot |
| `uiapduino` | UIAPduino のブートローダへの出入り |
| `catalog` / `names` / `interfaces` / `dump` / `fake` / `endpoint` | 能力の一覧と describe の形、表示、ハードウェアなしの偽物 |
| `target` | 上の主なものを 1 か所から import する入口（最初の版に合わせて書いた呼び出し側のため） |

## 使い方の例

```python
from oep_client.v1 import link, riscv, ch32_flash

hst = link.open_host("/run/board-identify/by-id/esp32-series-30eda0e31108")   # pipelining つき
hst.open(lease_ms=30000)
wire = riscv.Wire(hst, "oep.wire.rvswd")
conn, _ = wire.attach(halt=True)
dm = riscv.RiscvDm(hst, conn)
dm.reset_halt()
result = ch32_flash.program(hst, dm, open("sketch.bin", "rb").read(), ch32_flash.PROFILES["x035"])
dm.reset(confirm=True)
wire.detach(conn)
hst.end()
```

能力の一覧は `uv run python -m oep_client.v1 dump --port <probe>`（`--fake p4-x035` でハードウェアなし）。
実機での一通りの確認は ArduinoCore-CH32 の `tests/manual/oep_smoke/`（`oep_smoke.py`、`oep_probe_checks.py`）。

`oep_client.v0` は v0 の wire 形式を話す手動ツールのために残している。
