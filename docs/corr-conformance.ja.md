# corr u16の境界検査

core §4.1/§5.1のcorrは0を使わない循環番号である。hostはsession_idごとに番号を前へ進め、65535の次は1にする。
前進距離 `d = (new − last) mod 65536` が1〜32767なら新しい要求、それ以外（同じ番号とちょうど半周を含む）は記録に無ければresult_lost。
半周以上過去の記録は捨て、照合に使わない。SID 0の番号はsessionの履歴へ影響しない。異なるIDの新規sessionのopenが成功したときだけ再送状態を置き換える。

`CorrChecks`は明示したretention sampleの15項目に6契約を加えた21項目。通常coreの検査に加える補助検査で、一般probeに16 byteの保持サイズやsample資源opを要求するものではない。

- ASCENDING: 32767/32768/32769/65534へ半周未満の歩幅で進め、各新要求が一度だけ資源を作り、同一再送が保存結果を返す。
- HALF-GAPS: openのcorr 1から差32767の新要求は受理され、差32768/32769はresult_lostで実行されず、資源・履歴を変えない。
- WRAP: 65535の次は1で、sessionの資源と履歴を保つ。1から半周以上後ろの番号はresult_lostで実行されない。
- ZERO: corr 0は、不明fn、別session、SID 0も含めてmalformed。状態を変えない。
- FREE: SID 0の65535/1/32768/65535を一件ずつ完了させ、Sの履歴・leaseを変更しない。
- WRAP-END: 一周後のendで資源を解放し、終了後もendと直前の要求を保存結果で再送できる。別sessionのopen 1で履歴を置換し、次の要求を2から作る。

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

検査器`Checks.request()`の自動採番は65535の次を1にし、0を使わない。不正headerでpackingに失敗した場合は番号を消費しない。明示corr指定は0を含む異常系検査にも使う。

`full_conformance=false`。今回の対象は論理model上の番号境界と保存状態で、物理USB/TCP、並行処理、result_lostからの回復、他の保持サイズは未検査。SPECのwire型・番号、Arduino firmwareは変更していない。
