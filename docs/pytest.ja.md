# pytest（pytest-embedded-arduino-cli）で OEP を使う

[English](pytest.md)

OEP のプローブとその先のボードを、[pytest-embedded-arduino-cli](https://github.com/tanakamasayuki/pytest-embedded-arduino-cli) の試験で使います。
oep-client-python を `pytest` の追加の依存つきで入れると、プラグイン `oep_client.pytest_plugin` が自動で読み込まれます。conftest.py は要りません。

```
pip install "oep-client-python[pytest]"
```

プラグインが動くのは、試験のポートが OEP のプローブのときと、試験がプラグインの fixture を使うときだけです。

## 1. ポート

ポートは、pytest-embedded-arduino-cli のガイドのとおり `.env` に書きます。

```sh
# tests/.env
TEST_SERIAL_PORT_X035C8T6_S3=oep://30eda0e31108/x035c8t6-s3
TEST_SERIAL_PORT_PEER_PICO=oep://80f1b2d835d6/rp2040-x035
```

アドレスは `oep://<unit_id>/<口>` です（口はプローブのラベル `<口>.<線>` の前半。OEP の host ガイド §5.3）。

## 2. ロック

プローブは 1 台に 1 つのロックを持ちます（OEP の core §6.1）。プラグインは、pytest-embedded-arduino-cli のロックの名前を
`probe:<unit_id>` にします。同じプローブの口と経路（`?via=...`）は、どれも同じ名前になります。プローブ自身の USB シリアルの口も同じです。

- 1 つの試験の中で、DUT と相手が同じプローブに載っていても、ロックは 1 つを共有します。
- 別の試験は、そのプローブが空くまで待ちます。

## 3. 口の記録

その口の先に何がつながっているか（部品、相手の種類、ボードの不良など）を、試験の引数 `port_memo` で読みます。記録の形は、記録の持ち主
（試験のプロジェクト）が決めます。持ち主の名前は ini に書きます。

```toml
# pyproject.toml
[tool.pytest.ini_options]
oep_memo_owner = "io.github.example.mylib"
```

```python
def test_eth(dut, port_memo):
    if port_memo.get("parts", {}).get("eth", {}).get("to") != "lan":
        pytest.skip("eth is not wired on this port")
```

次の順に 1 つだけ使います。

1. `.env` の `OEP_PORT_MEMO_<unit_id>_<口>`（口の無いプローブは `OEP_PORT_MEMO_<unit_id>`。大文字で、`-` は `_`）。JSON のオブジェクト
   1 つを、一重引用符で囲んで書きます。丸ごと使い、メモ欄と項目ごとに混ぜません。
2. プローブのメモ欄の `<持ち主>/port/<口>`（oep-if-memo §4）。
3. どちらも無ければ `{}`。

両方あって中身が違えば、警告を出します（メモ欄を直すか、`.env` の変数を消します）。相手の口の記録は `peer_port_memo("<名前>")` です。
メモ欄を持たないプローブ（ほかのプラグインが `probe:<id>` の名前を返す WCH-Link など）は、`.env` だけを使います。

```sh
# tests/.env
OEP_PORT_MEMO_BC5F4B9807F7='{"parts": {"eth": {"to": "lan"}}}'
```

メモ欄に書くのは pytest の外の道具です（`oep_client.memo` の `Memo.write` を使う道具など）。

## 4. コンソール

pyserial は `oep://...` を開けます（`oep_client.pyserial`）。RP2040 のボードでは、SWD で読む SEGGER RTT がコンソールになります
（[swd.ja.md](swd.ja.md) §3.2）。スケッチは RTT を使います。

## 5. 書き込み

ポートが `oep://<unit_id>/<口>` で、プロファイルが RP2040 のボード（fqbn が `rp2040:`）のとき、プラグインがビルドの ELF をプローブの SWD で書きます
（pytest-embedded-arduino-cli の書き込みの hook）。arduino-pico の書き込みの方式は問いません。それ以外は arduino-cli の書き込みのままです。

## 6. セッションと UART

試験の引数 `oep_host` は、DUT のポートの先のプローブとの OEP のセッションです（`oep_host.host` が Host、`oep_host.touch()` が lease を保つ）。
60 秒より長く黙ると、セッションを失います。

`oep_uart.open(baud)` は、口のラベル `<口>.rx` / `<口>.tx`（OEP の host ガイド §5.3）の線に `oep.fixture.uart` を割り当て、`expect`、
`expect_exact`、`write` を持つチャンネルを返します（`dut` と同じ pexpect の形。既定の待ち時間は 30 秒）。

```python
def test_uart(dut, oep_uart):
    ch = oep_uart.open(115200)
    ch.write("ping\n")
    ch.expect("pong")
```

## 7. ほかの道具との相乗り

同じプローブを broker（OEP の host ガイド §7）が受け持っているときは、そのロックを分けてもらいます（`oep_client.brokers`）。
DUT のコンソールが platform 自前のモニタを通るとき（pytest-embedded-arduino-cli がポートを包むとき）は、そのモニタがプローブを持ち、
少し遅れて broker を公開します。`oep_host` は、それを ini の `oep_broker_wait` 秒（既定 10）まで待ちます。
