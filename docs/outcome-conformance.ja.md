# 採用を明示したwrite受付とdebug完了結果の検査

common §1.4のwrite、§3のdebug status/doneを採用したインターフェイスだけに適用する。名称やop番号から推測しない。全fnの宣言検査、資源寿命、OEP固有opの動作は別途必要。

| 契約 | 検査 |
| --- | --- |
| write | count=0、全受付success、先頭だけ受付partial、受付0のfailed。満杯でもcount=0はsuccess。acceptedはcount以下で、queueへの追加は入力の先頭accepted bytesだけ。 |
| queue | 明示した論理drainの後の容量回復と、未受付部分がqueueに入らないこと。受付は配送完了を意味しない。 |
| debug | status 0=ok、1=wait、2=line、3=fault、4=timeout、5=state。全進捗success、進捗0のfailed、途中進捗partial。受け付けた失敗はrejectedではなくcompleted。 |
| 配列 | doneに対応する結果だけを読み、その後の未知TLVを処理する。入力countを超えるdone、欠けた結果を拒否する。 |
| doneを持たないop | 成功payloadと、失敗時のcompleted failed / status u8＋TLVをop/outcomeで区別する。 |
| 再送 | queueの空きや失敗条件を変えても、同一要求は保存済みの応答bytesを返し、書込み・進捗を重複しない。 |
| 拒否 | malformedとSID 0のsession_requiredではqueue/進捗を変更しない。 |

今回の独自sample contractは、fn 1のwrite op 20（count u16＋data、応答accepted u16＋TLV）、fn 2のbatch op 16（count u16、応答done u16/status u8/done個のu32＋TLV）とsingle op 17（要求空、成功u32＋TLV、失敗status u8＋TLV）。queue容量4 byte、batch上限8、全opはlock必須。batch count=0はこのsampleではsuccessと定める。singleは共通失敗形の例で、標準のscan/attach/detachを実装したものではない。

`SampleOutcomeAdapter`は両部品の採用、fn/op対応、容量、上限、規範参照を明示する。`OutcomeModel`は独立した論理fixtureで、失敗条件の設定、queueのdrain、状態観測をadapter経由で記録する。各動作は新bootの明示logical restartから始める。物理UARTの配送やtargetの進捗を観測した結果ではない。

受信側の補助関数`decode_debug_counted`は未知statusでもdoneに従って同じ形で読む。`debug_succeeded`は既知のsuccess outcome / ok status / 全doneを満たす場合だけ成功とする。未知statusや未知outcomeを成功にしない。これは検査用sampleの分類で、production Hostへの統合や未受付部分の自動再送は行わない。

単体22件でoutcome誤り、先頭以外の受付、accepted過大、受け付けたdebug失敗のrejected化、done過大、結果不足、single失敗の形違い、cache再実行を検出する。未知status/outcomeの失敗分類、採用条件不足、選択SPECのstatus変更も確認する。仮想コアの最小拡張`Completed`でfailed/partialを応答とcacheへ保存する。新しいfailed/partial完了はleaseを更新し、cache再送は更新しないことも独立に検査する。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_outcomes import OutcomeChecks
from oep_client.conformance_outcome_sample import SampleOutcomeAdapter
from oep_client.virtual_outcome_model import OutcomeModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = OutcomeModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = OutcomeChecks(checks, SampleOutcomeAdapter(model)).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示する。公開検査は私的ベンチへの依存や探索を持たない。通常CLIへ自動追加しない。

`full_conformance=false`。物理配送/target進捗、独自statusの意味、debug接続資源の解放、production Host統合、別容量/結果形式、並行writer、OEP固有opは未検査。SPEC、Arduino firmwareは変更しない。
