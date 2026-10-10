# corr u16の境界検査

core §4.1/5.2はcorrを通常の符号なし整数として比較する。半周の差を使う循環番号ではない。同sessionの新要求は最大値より大きくし、0は使わず、一周させない。65535を使い切る前にend用の番号を残し、別session_idでopenする。SID 0の番号はsessionの履歴へ影響しない。同経路の未解決要求は、sessionが違ってもcorrを重複させない。

`CorrChecks`は明示したretention sampleの11項目に6契約を加えた17項目。通常coreの検査に加える補助検査で、一般probeに16 byteの保持サイズやsample資源opを要求するものではない。

- 32767/32768/32769/65534へ順に進め、各新要求が一度だけ資源を作り、同一再送が保存結果を返す。
- openのcorr 1から差32767/32768/32769の新要求を別sessionで作り、半周前後・ちょうど半周がすべて受理される。
- high=65534のとき、未使用の2/32767/32768/65533はresult_lost。資源・allocator・履歴・leaseを変えない。
- corr 0は、不明fn、別session、SID 0も含めてmalformed。状態を変えない。
- SID 0の65535/1/32768/65535を一件ずつ完了させ、Sの履歴・leaseを変更しない。未解決番号の重複は作らない。
- 最後の通常要求を65534、endを65535とし、資源解放と終了後の保存結果の再送を確認する。別sessionのopen 1で履歴を置換し、次の要求を2から作る。

`SampleRetentionAdapter`を明示して使い、raw交換、cache/high/last/deadline、資源とallocatorを観測する。再送の前に独立時計で5ms待ち、誤ったlease更新も検出する。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_corr import CorrChecks
from oep_client.conformance_retention_sample import SampleRetentionAdapter
from oep_client.virtual_retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleRetentionAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = CorrChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

`.env.example`の既存SPEC/共有lockを指定する。公開側から私的ベンチを探索しない。

検査器`Checks.request()`の自動採番は65535の次をValueErrorとして拒否し、内部カウンタを進めず、送信しない。不正headerでpackingに失敗した場合も番号を消費しない。これは検査器の保護であり、既存production Hostの自動session切替を実装したという意味ではない。明示corr指定は0を含む異常系検査にも使う。

`full_conformance=false`。今回の対象は論理model上の番号境界と保存状態で、物理USB/TCP、並行処理、production Hostの番号枯渇時の運用、result_lostからの回復、他の保持サイズは未検査。SPECのwire型・番号、Arduino firmwareは変更していない。
