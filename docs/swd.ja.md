# SWD のボードを書く・デバッグする

[English](swd.md)

OEP のプローブの SWD につないだ Arm のボードを、書いて、動かして、止めて調べます。道は 2 つです。

- **Arduino IDE から**: 「書き込み」ボタンと「デバッグ」ボタン。コアにも IDE の設定にも手を入れません。IDE のサーバーを 1 つ起動しておき、デバッグにはスケッチのフォルダに `debug_custom.json` を置きます。
- **OEP のホストから直接**: コマンド、gdb、pytest、Python。

対象は RP2040 のボード（Raspberry Pi Pico など、arduino-pico）のコア 0 です。

## 1. つなぐ

- 線は SWDIO、SWCLK、GND の 3 本です。ボードの電源はボード側で用意します。
- プローブの 2 本の channel に、口の名前を付けたラベル `<口>.swdio` と `<口>.swclk` を付けます。以後、ボードは `oep://<unit id>/<口>` で指します。

  ```
  oep settings set oep://<unit id> core channel.GPIO28.label=pico.swdio channel.GPIO27.label=pico.swclk --save
  ```

  どの channel につながったか分からなければ、`oep pins` で探せます（README の「ピンを探す」）。
- コンソールに UART を使うなら、ボードの TX をプローブの空いている channel につなぎます（3.4）。

## 2. Arduino IDE から

### 2.1 IDE のサーバーを起動する

```
pip install oep-client-python
python -m oep_client.ide oep://<unit id>/pico=rpipico --gdb-port 3333
```

- `=rpipico` は IDE に見せるボードの名前です（省くと `rpipico`）。口はいくつでも並べられます。口ごとに、ネットワークのポートが 1 つと、
  gdb のポートが 1 つ（`--gdb-port` から順に）できます。
- IDE のサーバーは、書き込みが来たときと gdb がつながっている間だけプローブを使います。ほかの時間は、ほかの道具がそのプローブを使えます。
- 起動の行に、口ごとの名前、UDP のポート、gdb のポートが出ます。

### 2.2 書き込む

1. IDE でボード（Raspberry Pi Pico など）を選びます。
2. 「ツール > ポート」で、ネットワークのポート `oep-<unit id>-<口> at <アドレス>` を選びます。IDE のサーバーを起動してから一覧に出るまで、数秒
   かかることがあります。
3. 「書き込み」を押します。パスワードを聞かれたら、空のまま OK します。

IDE のサーバーは、IDE が送る `.bin` を MD5 で確かめてから SWD で書き、読み返して確かめ、ボードを起動し直します。

仕組み: IDE のサーバーは口ごとに mDNS の `_arduino._tcp` を名乗り（TXT は `board`、`auth_upload=no`、`tcp_check=no`、`ssh_upload=no`）、
コアのネットワーク書き込み（arduino-pico の `tools/espota.py`）の受け手になります。名乗るアドレスは 1 つで、LAN のものです（`--interface` で
選べます）。

### 2.3 デバッグする

スケッチのフォルダに `debug_custom.json` を置き、IDE のデバッグをIDE のサーバーの gdb サーバーへ向けます。

```json
[
  {
    "configId": "rp2040:rp2040:rpipico",
    "servertype": "external",
    "gdbTarget": "localhost:3333"
  }
]
```

- ファイルは配列です。`configId` は、IDE が作るデバッグの設定の `configId` と同じ文字列にします。ボードの FQBN で、ボードのメニューを既定
  から変えていればその値も付きます。合わない要素は黙って無視されるので、IDE が一度デバッグを始めた後にスケッチのフォルダの
  `.theia/launch.json` に書く値を写すのが確実です。
- 「デバッグ」を押すと、IDE はスケッチを書き込み（gdb の `load`）、コアをリセットして止め、`main` で止まります。ブレークポイント、ステップ、
  一時停止、変数とレジスタの表示が使えます。
- gdb サーバーは 127.0.0.1 だけで待ちます。IDE はIDE のサーバーと同じ PC で動かします。
- デバッグの間に「書き込み」を押しても、gdb は切れません。IDE のサーバーは gdb のセッションの中で書き、コアだけをリセットして走らせます。

### 2.4 コンソール

ボードの UART を、プローブの USB シリアル（CDC）にそのまま流す設定を入れ、IDE のシリアルモニタでプローブのポートを開きます。

```
oep settings set oep://<unit id> oep.fixture.uart/0 passthrough.cdc0.rx=GPIO12 passthrough.cdc0.baud=115200 --save
```

- `GPIO12` はボードの TX をつないだ channel、`cdc0` はプローブの 1 つ目の CDC です（経路の名前は oep-spec core §13.3）。
- 設定はセッションに関係なく効き、`--save` で起動し直しても残ります。

### 2.5 できないこと

- コア 1 は扱いません（コア 0 だけ）。
- ハードウェアのブレークポイントは 4 つまでです（RP2040 の FPB）。flash の中のブレークポイントは、gdb が自動でハードウェアのものにします。
- ウォッチポイントはありません。

## 3. OEP のホストから直接

### 3.1 書く

```
python -m oep_client.rp2040_swd flash oep://<unit id>/pico sketch.ino.elf
```

ELF（flash の中の読み込む部分を並べる）か `.bin`（0x10000000 からの flash の中身）を書き、読み返して確かめ、ボードを起動し直します。

### 3.2 コンソール（SEGGER RTT）

UART の線が無くても、ボードの RAM の RTT のリングを SWD で読めます（スケッチが RTT を使うとき）。

```
python -m oep_client.rp2040_swd console oep://<unit id>/pico 10       # 10 秒の間、出力を表示する
```

pyserial からは、`oep_client.pyserial` を登録すると `oep://` を開けます。

```python
import serial
serial.protocol_handler_packages.append("oep_client.pyserial")
port = serial.serial_for_url("oep://<unit id>/pico")
```

### 3.3 gdb で

IDE のサーバーの gdb サーバーに、コアに付いてくる gdb（arduino-pico なら `arm-none-eabi-gdb`）でつなぎます。書き込みの受け手が要らなければ、
IDE のサーバーをこの用途だけに起動しても同じです。

```
arm-none-eabi-gdb sketch.ino.elf
(gdb) target extended-remote 127.0.0.1:3333
(gdb) monitor reset halt
(gdb) load
(gdb) monitor reset halt
(gdb) break loop
(gdb) continue
```

| monitor のコマンド | 動き |
|---|---|
| `reset halt`（`reset init`） | コアをリセットし、最初の命令で止める |
| `reset`（`reset run`） | コアをリセットして走らせる |
| `halt` | 止める |
| `resume`（`go`） | 走らせる |

gdb のつながっている間、IDE のサーバーはプローブのロックを持ち、5 秒ごとに keepalive を送ります。

### 3.4 pytest（pytest-embedded-arduino-cli）

試験の DUT や相手の Pico は、プラグイン `oep_client.pytest_plugin` が SWD で書き、RTT をコンソールにします。ポートは `.env` の
`TEST_SERIAL_PORT_*=oep://<unit id>/<口>` です。詳しくは [pytest.ja.md](pytest.ja.md) にあります。

### 3.5 Python から

```python
from oep_client import arm, link, rp2040

hst = link.open_host("oep://<unit id>")
hst.open(60000, owner="my-tool")
wire = arm.SwdWire(hst)
conn, dpidr, _ = wire.attach(targetsel=rp2040.TARGETSEL_CORE0, pins=(28, 27))   # SWDIO、SWCLK の channel
adi = arm.ArmAdi(hst, conn)
adi.power_up()
mem = arm.MemAp(adi, 0)
core = rp2040.core_of(mem)
core.halt()
print(hex(core.reg(15)))      # PC
core.resume()
wire.detach(conn)
hst.end()
hst.link.close()
```

- `oep_client.arm`: `SwdWire`（`oep.wire.swd`）、`ArmAdi`（`oep.target.arm-adi`）、`MemAp`、`CortexM`（停止、レジスタ、関数の呼び出し）。
- `oep_client.rp2040`: boot ROM での flash の書き込み（`program_and_verify`）と再起動。

### 3.6 ほかの道具とプローブを分ける

同じプローブを broker（OEP の host ガイド §7）が受け持っているときは、`rp2040_swd` と IDE のサーバーはそのロックを分けてもらいます
（`oep_client.brokers`: 環境変数 `OEP_BROKER`、利用者の `oep/brokers/<unit_id>.json`、相乗りの拡張 `oep.probe.share`）。broker が
いなければ、プローブを直接開きます。
