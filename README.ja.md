# OEP Python client prototype

破壊的変更を前提とするOEP client実験です。公開protocolまたは互換APIではありません。

最初のP0は、仮UART frameとcore messageについて次を確認します。

- endpoint confirmation
- offered function一覧の取得
- 16 bit correlationの照合
- transport破損とOEP rejectionの分離

V003実機操作は次段で追加します。数値割当とAPIは予告なく削除・変更します。

```sh
uv run pytest
```
