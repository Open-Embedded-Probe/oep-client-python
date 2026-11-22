# OEP Python client

[English](README.md)

Open Embedded Probe の host 側の Python ライブラリと `oep` の命令です。OEP のプローブにつなぎ、target を書いてデバッグし、
フィクスチャで周辺を試し、ロジックとアナログの波形を取り込みます。target の知識は host にある、という OEP の分担に従い、
CH32 の flash コントローラ、RAM ローダー、RP2350 の boot ROM、Cortex-M の debug レジスタなどはここに置きます。プローブは線と
DMI / DP・AP の転送しか知りません。

**実装する仕様: `tests/vectors/SPEC_COMMIT` にある oep-spec の commit**（oep-spec versioning §2 / §6: 凍結の前は revision 1 だけでは
形が決まらないので、実装は自分が実装する仕様の commit を名乗る）。OEP そのものは
[oep-spec](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/README.ja.md) にあります。変更の履歴は [CHANGELOG.md](CHANGELOG.md) にあります。

## インストール

```sh
pip install oep-client-python     # import するときの名前は oep_client
```

PyPI の `oep-client` は別のプロジェクトなので、配布名は `oep-client-python` です。Linux で USB の口を普通の利用者で開くには
udev の規則が要ります（[使い方ガイド](docs/guide.ja.md#linux-での-usb-の権限udev)）。

## 最小の例

```python
from oep_client import link, core, fixture

hst = link.open_host("oep://30eda0ea068b")   # プローブの unit id
try:
    core.take(hst, 30_000, owner="my-test")
    (led,) = core.channels_of(hst, ["GPIO10"])
    gpio = fixture.Gpio(hst)
    core.plan_apply(hst, [(gpio.fn, 1, led)])
    gpio.set([(led, gpio.OUTPUT_HIGH)])
finally:
    hst.end()
    hst.link.close()
```

```sh
oep dump --port oep://30eda0ea068b           # そのプローブの機能、ピン、設定
oep dump --virtual p4-x035                   # ハードウェアなしで、仮想ベンチのプローブ
```

## 文書

| 文書 | 中身 |
|---|---|
| [使い方ガイド](docs/guide.ja.md) | インストール、つなぎ方、ピンの割り当て、フィクスチャ、キャプチャ、デバッグと書き込み、設定 |
| [`oep` の命令](docs/cli.ja.md) | `oep dump` / `settings` / `pins` / `speed` / `linktest` ほか、残したセッション、TCP |
| [モジュール](docs/reference.ja.md) | 公開の API（`oep_client` のモジュールごとの中身） |
| [SWD のボードを書く・デバッグする](docs/swd.ja.md) | RP2040 / RP2350 のボードを Arduino IDE と gdb から OEP のプローブ越しに |
| [pytest のプラグイン](docs/pytest.ja.md) | pytest-embedded-arduino-cli から OEP のプローブを使う |
| [仮想ベンチ](docs/virtual-bench.ja.md) | ハードウェアなしで動くプローブと target（試験、CI、ほかの host の開発） |
| [docs の一覧](docs/README.ja.md) | 実機の試験、適合検査、テスト方針を含む全部の文書 |
| [貢献の手引き](CONTRIBUTING.ja.md) | 試験、番号の表の写し方、リリース |

## ライセンス

[LICENSE](LICENSE)
