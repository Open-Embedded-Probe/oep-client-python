# 混雑時のsession再送・拒否cacheの補助検査

core §4.3/5.2/6.1を、[経路別受付モデル](pipeline-conformance.ja.md)の上で検査する。受付枠は接続ごと、最後に成功した新規sessionの再送履歴はprobeに一つ。拒否の順は見出し、再送、session、そのほかの状態判定で、window_exceededは最後の段階に属する。新しいsession要求が再送判定を通った場合、rejectedも記録する。単に受付が混雑しているためにこの順序を飛ばさない。

超過要求を落として応答しない選択もSPEC上許される。今回の検査は`reject-overflow`を選ぶ明示sample用であり、一般probeに超過拒否を必須化しない。範囲外刺激もfixtureの拒否方針を検査するために意図して送る。通常hostが上限を超えてよいという変更ではない。

`ReplayPressureChecks`はpipelineの9項目、追加instrumentation前提1項目、次の5契約を合わせた15項目。fn 1のsample create(kind u8)で実際に資源を作り、返された非zero u16 ID、raw応答、観測した資源一覧を比較する。

- 3つのcreateで受付枠を満たし、応答が止まっている間に最初の要求をそのまま再送する。同じ応答を返し、資源は3個のまま。送信再開後の再送もlease/cache/資源状態を変えない。
- 3つの要求で枠を満たし、新corrのcreateを拒否する。枠が回復した後にも同一要求には同じwindow_exceededを返し、実行しない。同corrの変更要求はmalformed、新corrなら1回だけcreateが成功する。
- 応答待ちの受付枠を満たした後、保存済み要求、変更された同corr、unknown fn/op、corr 0、別session、session 0の資源要求を送る。混雑より先の判定で答え、保存済み結果を返す。前段の拒否でlease/履歴/資源を変えない。
- peerからsession 0で、Sの保存済み要求と同じcorrのclockを12回送る。Sの履歴を消費・置換・更新しない。Sの再送はprimaryだけで行う。
- peerでboot/holderを再確認し、別ID Tの新規openをforceする。Sの資源と履歴が消え、共有cacheはTのopenから始まる。Sの旧要求は元のprimaryでlockedとなり、Sの保存済み成功は返らない。Tだけをpeerでendする。

すべてのsession S要求はprimaryに固定する。peerへSを移して共通cacheを試す構成にはしない。各ケースのSは乱数で選び、TもSとは異なる乱数。forceは自分で保持したSにだけ行う。実機adapterへの適用には別の設備計画が必要。

instrumentationは既存route adapterの`state()`にholder、last、high、deadline、cache、resourcesを明示して返す。要求/応答の全bytesとdeadlineそのものを比較し、単なる「再送回数」や成功カウンターで代用しない。sampleの資源opが宣言されていること、観測能力・フィールドの有無をsession操作前に確認する。

公開coreモデルは任意の`admission_reason`を受け、見出し/再送/session判定後にだけ拒否を適用する。通常のcore/旧profileには理由を渡さない。pipelineモデルが独自に拒否frameを作る経路を廃し、拒否も同じcoreの履歴管理へ通す。公開wire opの追加やSPEC変更ではない。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance_replay_pressure import ReplayPressureChecks
from oep_client.conformance_pipeline_sample import SamplePipelineAdapter
from oep_client.virtual_pipeline_model import PipelineModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SamplePipelineAdapter(PipelineModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-routes-1')
    report = ReplayPressureChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示する。公開テストから私的ベンチを探索しない。raw交換・全制御・時刻・宣言・SPEC/検査器/モデルhashを新規artifactへ保存する。常に`full_conformance=false`。

実TCP/USB、silent lossからの回復、並行実行、再起動、host scheduler、大きな要求/応答の保持上限は未検査。leaseを物理的な送信完了から数え始める検査も別途必要。このsampleのcoreは処理完了時にleaseを更新するため、送りかけ結果が残る間のleaseタイミングの適合を今回の結果から主張しない。
