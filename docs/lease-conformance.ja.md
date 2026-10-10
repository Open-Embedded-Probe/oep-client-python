# 応答送信とleaseの補助検査

core §6.1は、session判定を通った持ち主の要求への応答を送った時からleaseを数え直す。rejectedも含むが、見出し・再送・別sessionの拒否は更新しない。要求の実行中はleaseを数えない。送信待ちは実行と分け、以前の期限を維持する。

`LeaseChecks`は[pressure検査](replay-pressure-conformance.ja.md)15項目、時計/観測の前提1項目、次の6契約で22項目。明示したlogical writerで応答全体の完了を制御する。物理USB/TCPでの送信完了を証明する検査ではない。

- clock応答を止めて待ち、7 byteだけ送って待つ。処理完了・待機・部分送信ではdeadlineを変えず、全応答の完了時にlease_ms先へ更新する。
- session判定を通ったmalformed拒否は、全応答の完了時に更新する。保存した拒否応答の再送は更新しない。
- 成功の再送、session 0の照会、見出しでの拒否、別sessionへのlockedは更新しない。
- 結果が送信待ちでも以前の期限を過ぎれば失効し、資源/購読を解放する。遅れて送る成功応答とその再送は、終了したsessionを復活させない。新corrのkeepaliveはno_sessionとなる。
- Sの応答をprimaryで止めたまま、boot/holderを再確認してpeerで別ID Tをforceする。後で完了したSの応答はTのdeadlineを変えない。Tだけをpeerでendする。
- endは状態処理で資源/購読を解放する。先行成功応答やendの送信完了/再送でsessionを復活させない。

lease=1000msで検査し、待機は100〜1050ms。adapterの独立したclockで各waitが実際に指定時間以上進んだことを確認する。`timing_state()`のdeadlineとprobeモデルのnow_msを、送信再開の前後で取得する。全応答のdeadlineは「再開直前のnow+lease」から「再開後のnow+lease」の範囲に入り、更新しないケースでは以前のdeadlineそのものと一致する。時刻・全制御・raw要求/応答・stateを記録する。瞬時の時計の完全一致を実時計へ要求しない。

sample adapterは`SamplePipelineAdapter`にclock_ms、wait_ms、timing_stateを加える。既定では独立したmonotonic時計と実sleep、単体では明示した仮想時計を使う。clockとwaitは外から指定でき、モデルのdeadlineに合わせて待機時間を捏造しない。既存`.env.example`のSPEC/共有lockを使い、私的ベンチ・設備の探索は加えない。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance_lease import LeaseChecks
from oep_client.conformance_lease_sample import SampleLeaseAdapter
from oep_client.virtual_pipeline_model import PipelineModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleLeaseAdapter(PipelineModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-routes-1')
    report = LeaseChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

公開coreモデルは通常の同期APIで全応答を返す時点を送信として扱う。明示pipelineモデルはdefer_sendを使い、応答ごとの更新ticketを単一writerに渡す。再送/前段の拒否にはticketを作らない。ticketはsession IDとロック世代に結びつき、end・期限切れ・forceで無効になる。全応答を書いたとき、まだ同じ世代のsessionが保持していればdeadlineを更新する。閉じた経路では未完了ticketを破棄する。

実行時間は以前のdeadlineへ足し、実行中の時間をleaseから除外する。処理終了でlease全体を更新する方式にはしない。同じsessionの新corr openはlease値を変更し、資源/履歴を残し、全応答で更新する。同一openの再送は更新しない。これらの長い実行・経路切断・同session openは仮想時計の追加単体検査で、記録した実時計22項目とは区別する。

`full_conformance=false`。実際のUSB/TCP送信完了、並行処理、実時計での長いop、再起動、host scheduler、silent loss回復、大きな要求/応答の保持上限は未検査。公開wire opやSPECの変更ではない。
