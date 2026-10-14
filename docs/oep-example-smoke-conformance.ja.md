# OEP固有の代表シナリオ検査

`OepExampleSmokeChecks`は、GPIO・受信専用UART・raw ARMを題材に、独自拡張の試験にも使える「準備→要求→応答→作用→解放」の例を提供する。
コア検査や全拡張共通の検査へ、これらの固有操作を必須として混ぜない。

```python
from oep_client.conformance import spec_identity
from oep_client.conformance_core_smoke import CoreSmokeChecks
from oep_client.conformance_oep_smoke import OepExampleSmokeChecks

registry, spec = spec_identity(spec_checkout)
core = CoreSmokeChecks(send_message, registry, expected_unit, spec=spec)
report = OepExampleSmokeChecks(
    core,
    pins=selected_pins,  # gpio: 2 channels, uart_rx: 1 channel, swd: 2 channels
    observe_resources=observe_selected_resources,
    resources_released=selected_resources_are_released,
    feed_uart=send_selected_uart_data,
    functions=selected_functions,  # optional: name -> fn for multiple instances
).run()
```

pinsと各callbackは呼び出すプロジェクトが明示する。私的benchのファイルや固定配線を参照しない。
同名のinstanceが複数あれば、現在のlistから選んだfnをfunctionsで指定する。
これは実行すると指定GPIOを設定し、UARTのplanを作り、SWD attachを行う検査である。明示した設備だけを選び、排他とframingは呼び出す側で用意する。
未選択のtargetを探したり、他のセッションをforceで奪ったりはしない。

SPECのcoreと、fixture/plan/debug/commonの本文SHAを確認してから送信する（レビュー済みはoep-spec a09fa45）。
選択したOEPの必須操作はopsにあることを確認する。不在なら準備不成立を失敗として記録し、任意機能と混同しない。

GPIOはplan/set/readを行い、後ろに未対応modeを持つbatchを実行前に拒否して旧状態を保つことを確認する。
出力low/highが宣言されていればその変化を使い、mode0だけの最小GPIOでも同じ構造で確認できる。
同値設定の再適用などを確かめるには、levelだけでなく設定作用の回数などを独立に観測する必要がある。

UARTはRXだけのplan、configure、明示したAB刺激、2回の非消費readを扱う。
受信はconfigureから始まるため、刺激はconfigureの後に与える。configure前のstatusがbaud 0・format 0（未configureの印）であること、
configure後のstatusがconfigureの返した実際のbaudと8N1であることも確認する。
marks/writeが無ければ、それぞれfrom3のunsupportedと不在writeのunknown_operationを確認する。任意操作の不在を違反にはしない。
刺激callbackが無ければUARTはnot_run、全体はincompleteであり、PASSや設備不足skipにしない。

ARMはSWD attachで得たconnectionを別target fnのtransferへ渡し、DP identityとACK/進捗を確認する。
attachは固定部分`method(0)、swdio、swclk、max_speed`で送り、connectionはu32として読み、transferの先頭へu32で置く。
固定応答がmax_frameへ入らないread batchはtargetへ触らず拒否する。
session終了後に新SIDで旧connectionを使い、no_resourceと作用不変を確認する。
read_block/write_blockを必須にせず、新しい世代番号やlarge result bufferを要求しない。

observe_resourcesは、選択したplan/pin設定/buffer/connection/target作用などのJSON互換snapshotを返す。
拒否でも変わりうるcoreのcorr/lease状態や、欠番にしてよい次の資源IDは、作用不変のsnapshotへ混ぜない。
resources_releasedは選択した資源の解放を独立に確かめる。UART positionのように起動内で保持する値や、外部targetへの完了済み作用をゼロへ戻す意味ではない。

失敗後は追加シナリオを止め、未終了SIDを結果へ残す。通信が壊れた場合も無条件なcleanup要求を送らず、呼び出す側が状態と経路を確認して回復する。
未提供のinterface全体はその例を適用外にする。この検査は全operation、全任意機能、全resource graph、電気条件、完全なOEP固有準拠を証明しない。
reportのscope/uncheckedとfull_conformance=falseを結果と一緒に扱う。
