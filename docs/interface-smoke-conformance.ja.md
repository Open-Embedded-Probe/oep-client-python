# 全拡張に使うインターフェース大枠検査

`InterfaceSmokeChecks`は、標準OEPと独自拡張に共通する選択した契約を確認する。
新しいcore contextを使い、旧検査のcore SHAガードを解除しない。

```python
from oep_client.conformance import spec_identity
from oep_client.conformance_core_smoke import CoreSmokeChecks
from oep_client.conformance_interface_smoke import InterfaceSmokeChecks

registry, spec = spec_identity(spec_checkout)
core = CoreSmokeChecks(send_message, registry, expected_unit, spec=spec,
                       observe_effects=observe_selected_effects)
report = InterfaceSmokeChecks(core).run()
```

callback、排他、framing、待ち時間、観測、結果保存は[コア大枠検査](core-smoke-conformance.ja.md)と同じ。
機器を探索・接続する処理や、別のプロジェクトの設定への依存はない。

confirm/core describe/listを準備し、各fnの名前、ops、共通tagの固定幅、宣言するpin候補、instance順、繰返しdescribeの不変を確認する。
未提供操作はfnごとに代表1個を選び、sessionなしでunknown_operationとなることを確認する。
これにより、未提供操作への拒否をsession_requiredより先に行う契約を扱う。
観測callbackがあれば拒否前後の作用を比較して保存し、無ければ未観測をreportへ残す。

名前付き機能がない場合はそのfnごとの検査を適用外とし、準備caseの結果を保つ。
各fnの前提caseに失敗したら、その後の追加要求を止める。失敗や準備不成立を設備不足としてskipしない。
checker/core/helper/再利用した宣言検査のSHA、SPEC、要求/応答、観測を結果に記録する。

標準OEPの必須操作、提供する操作の動作、resources/終了時の寿命、全部の未提供op、全paging条件、電気的なpin挙動は、この検査の範囲外である。
UARTでmarksやwriteが無くても、それを共通検査の失敗条件にしない。UART自身のconfigure/read/statusなどは固有の検査が確認する。
独自の1操作だけの拡張も、同じ共通検査を使える。
reportは常にfull_conformance=falseで、3段階目を実行したとはしない。
