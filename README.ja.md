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
uv run python -m oep_client --port /dev/ttyUSB0 --program-page64 0x08003fc0 <128桁のHEX>
```

2026-09-19、無印ESP32 prototypeとの間でendpoint revision 1、最大message 64 byteおよび
V003 control/memory/flash、fixture GPIO/UART/I2C/SPIの仮reference 7件を取得しました。

2026-09-20、実装済みfunctionだけを公開するよう修正し、TargetControl `0x0101`を取得した。
OEP requestとして状態取得、user mode正規化、製品bootloader移行が成功し、boot移行後に
Windows側で`1209:b803`の再列挙を確認した。

同日、TargetMemoryのbounded readを追加し、`0x08000000`から16 byteを実機取得した。現在の
clientは4 byte aligned、4～32 byteだけを受け付ける。これはprototype制約である。

同日、TargetFlashの64-byte page programを追加した。`0x08003fc0`への書込みと独立read-backが
一致し、範囲外要求が開始前にrejectedとなることを確認した。page dataを一つの論理requestへ
載せるため、相手が通知するmaximum messageは96 byteになった。

FixtureGpioのdigital read clientも追加し、ESP32側で許可したUIAPduino配線だけを実機観測した。
