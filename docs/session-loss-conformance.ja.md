# end/openの結果喪失からの回復を検査する

`SessionLossChecks`は保持検査11項目、制御前提1項目、回復9項目の21項目。core §4.1/5.2/6/9に基づき、endのcorr 65535と新sessionのopen 1について、保存した要求を同じ論理経路へ同じbytes/corrで再送する。production Hostへ回復を組み込む変更ではない。

`SampleSessionLossHost`はwireだけで判断する。要求保持16 byte/履歴8件の明示sampleで、同経路の未解決要求は対象の1件だけ。接続を保ったまま要求または全応答を失い、操作待ち0ms・論理転送0msに対して1050ms待つ。reader barrierで未受信message/creditが空であることを確認してから、個体/bootを読み、元の要求を再送する。barrier不成立やboot変更では再送しない。経路を移したり、forceや新corrで変更操作を再実行したりしない。

| 条件 | 判断 |
| --- | --- |
| end応答を失う | 再送で保存された成功を確認し、SID 0のlock_stateとsample inventoryで解放も確認する。65535を増やさない。 |
| end要求17 byteの応答を失う | 再送はresult_lost。解放を観測しても元のoutcomeはunknown。 |
| end要求が届かない | 同一要求の再送が初回実行となる。資源を解放し、65535で完了する。 |
| open応答を失う | 保存された成功のlease/boot/TLVを確認し、Sのclockを新corrで読んで現在の所有権を確認する。再送だけではleaseを更新しない。 |
| open要求18 byteの応答を失う | 再送はresult_lost。S clockが成功すれば現在の有効性を確認できるが、元のoutcomeと実際のlease値は不明のまま。 |
| open要求が届かない | 同じopen 1を初回実行し、応答と現在のSを確認する。 |
| open後にlease期限切れ | 保存されたopenの成功を再取得できても、S clockがno_sessionなら利用を再開しない。 |
| boot変更 / reader barrier不成立 | 再送・次openを送らず停止する。 |

openの確認はclock/boot確認/clockの順で行う。読み直し中のboot変更では観測を破棄する。lock_stateがheldでもSの所有権とは判断しない。読み出しはatomic snapshotではなく、並行writerがいないfixture条件に限定する。古い資源を再bindingせず、操作結果と現在の状態を分けて記録する。

単体では新corrの再送、状態から成功を推測、未知leaseの捏造、期限切れ無視、boot変更無視、barrier無視の6欠陥を検出する。入力/制御能力不足、読み直し中のboot変更、別sessionへforceされた後の旧open再送も検査する。fixture cleanupのendは回復hostが返した判断の後に別途行う。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_session_loss import SessionLossChecks
from oep_client.conformance_session_loss_sample import SampleSessionLossAdapter
from oep_client.virtual_retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleSessionLossAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = SessionLossChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示する。私的ベンチへの依存や自動探索は不要。既存の[切替検査](rollover-conformance.ja.md)は応答喪失時に停止する負例として保持し、この回復検査と分ける。

`full_conformance=false`。物理reader停止/再同期、TCP再接続、複数pendingの放棄後の回復、production Host統合、任意の並行writerや拡張の副作用は未検査。TCP切断後の新接続へ旧Sを移せるという保証はない。SPEC・仮想probe・Arduino firmwareは変更していない。
