# 保持欠落・履歴の追い出しとsession/boot寿命の補助検査

core §5.2/6/9を、明示した16 byte / 8件の[retention sample](retention-conformance.ja.md)で検査する。要求欠落、応答欠落、記録追い出しをそれぞれ作り、期限切れと異なるIDのforceでの扱いを分ける。終了と同じboot内のID継続、boot変更時の無効化は別の契約。

`RetentionLifecycleChecks`はretentionの11項目＋明示reset制御の前提1項目＋7寿命契約で19項目。reset能力はsession操作の前に確認し、`reset_scope='logical-model'`だけを許す。実機の再起動/転送をこのadapterへ渡さない。

- 期限切れ×3状態: 1000msのleaseで資源と購読を作り、1050ms待つ。資源/購読は消えるが、cache/high/last/boot/次の資源番号は変わらない。同一再送はresult_lost、要求を保持している応答欠落では変更した同corrはmalformed。新corrのkeepaliveはno_session。
- 期限切れ後の別ID open: 新規openから履歴を始める。資源は新sessionへ渡さず、資源番号は同じbootの次の番号を使う。
- force×3状態: 自分のSのboot/holderをpeerで再確認し、別ID Tをforceする。Sの資源/購読/履歴を捨て、cache/highはTのopenから始まる。Sの旧要求は元のprimaryでlockedとなり、Tの状態を変えない。Tの資源番号は同じbootの次の番号。Tだけをpeerでendする。
- 論理再起動: 要求欠落・応答欠落・保存済み成功、送信待ち応答、受理した未実行要求を置いてから、明示した異なるu32 boot_idでsampleを作り直す。全履歴、session、資源、購読、送信枠/待ち結果を消し、uptimeと資源番号を再開する。両経路のconfirm/identityで新bootを確認し、旧Sの要求はno_sessionとなる。旧openは再送せず、別IDの新sessionを開いて資源番号1から作る。

Sの要求はprimaryに固定する。peerはsession 0照会と別ID Tだけに使い、Sを別の接続へ移さない。終了したIDを再使用しない。再起動後の旧open再送はsessionを新規に作り得るため、この検査でも回復手順でも行わない。

`SampleRetentionLifecycleAdapter`は既存adapterへ論理restartだけを追加する。modelのboot_idはstateで観測し、restartの返答だけで成功を判断しない。confirm/clockでも実際のbootを確認する。独立monotonic時計で待機を測り、state/cache/allocator、全raw交換・制御時刻・restart入力/出力を記録する。

`RetentionModel.restart()`は異なるu32 boot_idのみ受け、同一boot・範囲外・boolは拒否する。sampleのbootを確実に変えるための制御で、実際のboot乱数源を模擬検証したものではない。既存core/通常profileや物理設備の再起動入口を追加しない。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance_retention_lifecycle import RetentionLifecycleChecks
from oep_client.conformance_retention_lifecycle_sample import SampleRetentionLifecycleAdapter
from oep_client.virtual_retention_model import RetentionModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleRetentionLifecycleAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = RetentionLifecycleChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示する。私的ベンチ・設備・ポートを探索しない。reset capabilityがない設備やphysical scopeでは操作を始めずFAILとする。`.env`へ実機reset設定は追加しない。

常に`full_conformance=false`。物理的な再起動/接続回復、並行処理、hostのresult_lost回復方針、他の保持上限、boot_idの乱数源は未検査。boot_id=0も有効で、uptimeの再開は追加の仮想時計単体でも確認する。
