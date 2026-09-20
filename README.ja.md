# OEP Python client prototype

破壊的変更を前提とするOEP client実験です。公開protocolまたは互換APIではありません。

現在のP1 prototypeは、仮UART frameとcore messageに加えてV003 TargetControlを確認します。

- endpoint confirmation
- offered function一覧の取得
- 16 bit correlationの照合
- transport破損とOEP rejectionの分離
- target状態取得、user mode正規化、製品bootloader移行
- requestの拒否と、開始後に失敗したcompleted outcomeの分離

数値割当とAPIは予告なく削除・変更します。

```sh
uv run pytest
```

実機のP0確認:

```sh
uv run python -m oep_client --port /dev/ttyUSB0
uv run python -m oep_client --port /dev/ttyUSB0 --target status
uv run python -m oep_client --port /dev/ttyUSB0 --target normalize-user
uv run python -m oep_client --port /dev/ttyUSB0 --target bootloader
uv run python -m oep_client --port /dev/ttyUSB0 --read-memory 0x08000000 16
```

2026-09-19、無印ESP32 prototypeとの間でendpoint revision 1、最大message 64 byteおよび
V003 control/memory/flash、fixture GPIO/UART/I2C/SPIの仮reference 7件を取得しました。

2026-09-20、実装済みfunctionだけを公開するよう修正し、TargetControl `0x0101`を取得した。
OEP requestとして状態取得、user mode正規化、製品bootloader移行が成功し、boot移行後に
Windows側で`1209:b803`の再列挙を確認した。

同日、TargetMemoryのbounded readを追加し、`0x08000000`から16 byteを実機取得した。現在の
clientは4 byte aligned、4～32 byteだけを受け付ける。これはprototype制約である。連続readでは
一部requestが`completed/failed`となることも確認しており、SWDIO backendの安定性は未確立である。

同日、TargetFlashの64-byte page programを実験した。直後のverifyと後続requestのread-backが
一致しないcase、およびreset後にbootへ移行できない状態が判明したため、ESP32 prototypeは
TargetFlashをoffered functionから外している。clientコードは失敗解析用に残すが、現在の実機で
利用可能な操作ではない。96 byteのmaximum messageはendpointの受理容量として維持する。

上記はV003/SWIO backendの当時の結果である。2026-09-20に追加したESP32-P4/X035 RVSWD
backendはTargetFlashを公開し、64-byte単発、差分全image、reset後全域verifyまで実機確認した。

FixtureGpioのdigital read clientも追加し、ESP32側で許可したUIAPduino配線だけを実機観測した。

FixtureUartのconfigure/write/read clientを追加した。115200 bpsでV003の`PING`/`PONG`往復を確認し、
peerが返す`ERROR command`もUART transfer自体のfailureへ変換せず取得できた。

最新版fixture imageではUART `DOUT`とFixtureGpioを組み合わせ、target pin 7→ESP32 GPIO27、
target pin 9→ESP32 GPIO14のLOW/HIGH/LOWを確認した。再実行用smoke testは次で起動する。

```sh
uv run examples/uiapduino_fixture_smoke.py --port /dev/ttyUSB0
```

## CH32X035 image操作

このCLIはまだ破壊的prototypeであり、既定値`0x08000000`、63,488 byteはCH32X035専用である。
最初に現在のimageを退避する。

```sh
uv run python -m oep_client \
  --port /run/board-identify/by-id/esp32-series-30eda0e31108 \
  --backup-flash original.bin
```

Arduino CLI等が生成したraw `.bin`を書き込む。入力末尾からflash終端までは`0xff`で埋め、現在値と
比較して異なる64-byte pageだけを書き込む。`--destructive`を省くと実行しない。書込み後はtargetを
software resetし、63,488 byteを別OEP readで全域verifyする。

```sh
uv run python -m oep_client \
  --port /run/board-identify/by-id/esp32-series-30eda0e31108 \
  --program-image build/sketch.ino.bin --destructive
```

書込みせず照合だけ行う場合:

```sh
uv run python -m oep_client \
  --port /run/board-identify/by-id/esp32-series-30eda0e31108 \
  --verify-image build/sketch.ino.bin
```

別容量のtargetでは`--flash-base`と`--flash-size`を必ず明示する。現在のX035 bit-bang実装では
全域read/verifyに約240秒、変更109 pageのprogramに約298秒かかるため、既定timeoutは45秒とした。
operation failureは同一64-byte pageを最大2回再送する。未回復の物理pageがあるとprobeは別pageを
拒否する。probe自身をresetするとRAM上の回復cacheを失うので、その場合は既知の完全imageからの
再書込みを行う。
