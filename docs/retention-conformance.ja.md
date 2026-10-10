# 再送cacheの保持上限とresult_lostの補助検査

core §5.2は直近max_inflight個以上の記録とcorrの最大値を保持し、要求/応答bytesの保持にサイズ上限を認める。保存済み要求は全bytesで比較し、変更ならmalformed。同じ要求でも応答を保持できなければresult_lost。要求そのものを保持できない場合や、記録が追い出されて最大corr以下の場合もresult_lost。どの場合も再実行・lease更新・資源変更をしない。

一般probeに具体的な保持サイズを要求しない。今回のfixtureはmax_frame=64に対し保持上限16 byte、履歴8件を明示した`virtual-retention-1` / `io.github.open-embedded-probe.retention` revision 1。通常のcore/pipelineの保持上限72 byteを変更しない。経路の宣言値はpipeline sampleと同じ64/80/3、peerは64/96/2。履歴8件は両経路のmax_inflight以上。

sample fn 1/2のown create(op 16)はkind u8、reply_size u8、[TLV]を受け、resource_id u16、[TLV]を返す。kindは1/2、reply_sizeは応答message全体の長さ7/16/17。16/17では非critical tag 0x40のpaddingを足す。要求も非critical TLVで16/17 byteに調整する。いずれもmax_frame内で実行可能な要求で、保持上限は受付上限とは別。

`RetentionChecks`はidentity/幾何条件＋8契約＋primaryの2 IF宣言で11項目。

- 要求16 byte: 全要求と応答を保持し、同一再送を返す。変更した同corrはmalformed。
- 要求17 byte: 要求の欠落markerと小さい応答を保持する。同一/変更要求はresult_lostで、資源を増やさない。
- 応答16 byte: 全要求と応答を保持し、同一応答を返す。
- 応答17 byte: 全要求と応答欠落markerを保持する。同一要求はresult_lost、変更した要求はmalformed。
- 件数上限: createの後に12件のkeepaliveを送り、最初の記録を追い出す。直近3件以上は保持する。追い出された要求と、意図して空けた未使用の古いcorrはresult_lost。新corrでは1回だけcreateが成功する。
- 拒否記録: 17 byteの不正kind要求をmalformedとして記録し、要求欠落による再送はresult_lost。失敗と再送で資源番号を消費しないsample方針を確認する。
- end: 資源を解放しても欠落markerと履歴を残し、再送はresult_lost。sessionを復活させない。
- 同session新corr open: 資源と応答欠落markerを保持し、再送はresult_lost。別sessionを使わず、Sはprimaryに固定する。

`state()`のcacheにはcorrと実際の要求/応答bytesまたはNone markerを含め、deadline、high、last、資源一覧、next_resource_idも観測する。再送前後は状態そのものが一致する。5ms待機を独立時計で確認し、deadline誤更新を見逃さない。新corrの作成が次の番号を一度だけ返すことも確認する。単なる成功回数の自己申告で代用しない。

`SampleRetentionAdapter`は既存の明示lease adapterに`retention_state()`を加え、保持サイズと件数を返す。機能/型/幾何条件/個体/資源opの宣言をsession操作前に確認する。これはモデルinstrumentationで、実機に新しい公開opを追加するものではない。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance_retention import RetentionChecks
from oep_client.conformance_retention_sample import SampleRetentionAdapter
from oep_client.virtual_retention_model import RetentionModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleRetentionAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = RetentionChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示し、公開側から私的ベンチを探索しない。全raw交換、cache/allocator snapshot、待機/制御時刻、SPEC/検査器/モデルhashを新規artifactへ保存する。

`full_conformance=false`。実機、並行処理、再起動、hostがresult_lostから状態を読み直して回復する方針、記録した実時計実行での期限切れ/forceと保持欠落の組合せ、ほかの保持上限は未検査。保持16 byte / 8件と、失敗時に番号を消費しない方針はsample条件で、一般probeへの追加規範ではない。
