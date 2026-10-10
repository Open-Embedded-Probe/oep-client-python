# 経路ごとの要求受付と回復の補助検査

SPEC core §4.4/7.1から、経路ごとのmax_frame、window、max_inflightと要求/応答の順序を検査する。windowは見出しを含む未解決要求のOEP message bytesの合計で、COBS/CRC/lengthの包みを含めない。受けたTCP接続はそれぞれ独立した枠を持つ（transports §3）。再送cacheはprobe全体に一つで、今回の受付枠とは別。

hostは宣言した上限を守る。**超過要求へのwindow_exceeded応答は任意**で、容量を超えた要求には応答がない場合も許される。従って超過拒否の検査をすべてのprobeに課さない。今回の補助検査は`reject-overflow`を明示した論理モデル専用。実機wireの上限検査、silent lossを選ぶprobe、実際のhost schedulerは別に扱う。

サンプルは既存routes interfaceを使い、primaryはmax_frame=64 / window=80 / max_inflight=3、peerは64 / 96 / 2をconfirmで宣言する。primaryへ10 byte要求3つを送るとcountだけが上限、64 byteと16 byte要求を送るとwindowだけが上限に達する。clockに非critical TLVを付けてサイズを調整し、要求に副作用を加えない。各経路にsession 0のみを使い、sessionを別経路へ移さない。

要求を受理するとその経路の枠を予約し、**このサンプルでは応答message全体を書き終えるまで**保持する。処理完了だけで解放せず、途中まで送った応答も未解決とする。これは保守的なモデル方針で、SPECに新たな内部bufferの解放時点を定める変更ではない。送信完了後には枠を戻す。超過要求は受理せず、受信順にwindow_exceededを返す。通知queueの上限とは別管理。

`PipelineChecks`はidentity/宣言と6動作契約、primaryの2 IF宣言を含む9項目。peerの個体/boot/宣言も照合する。

- count境界: 10 byte×3要求を受理し、writer停止中も枠を保持する。
- window境界: 64+16 byteの要求を受理し、見出し/TLVを含む80 byteを数える。
- 独立性: primaryを満杯にして止めてもpeerは自身の上限まで受理・応答でき、再度受理できる。
- count超過: byte枠には余裕がある4つ目を拒否し、新corrの次要求は回復後に受理する。
- byte超過: count枠には余裕がある3つ目を拒否し、新corrの次要求は回復後に受理する。
- 部分応答: 7 byte送っても枠を保持し、全応答を送ると解放する。

各応答の経路・corr・受信順・outcome・boot・TLVをraw bytesで検査する。単なる残量の自己申告ではなく、instrumentationの`pending(route)`が返す受理済み未解決要求そのものを、検査側の要求列と比較して数とbyte数を計算する。ただしモデルのinstrumentationを観測する検査であり、実機bufferの直接測定ではない。

adapterは`SampleRouteAdapter`の制御/trace能力に加え、明示した`admission_policy='reject-overflow'`と`pending`を提供する。service観測予算は既存の1〜1000ms（サンプル100ms）を使う。全入力/出力、要求/応答、制御時刻を記録し、公開opを新設しない。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance_pipeline import PipelineChecks
from oep_client.conformance_pipeline_sample import SamplePipelineAdapter
from oep_client.virtual_pipeline_model import PipelineModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SamplePipelineAdapter(PipelineModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-routes-1')
    report = PipelineChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

SPEC/共有lockは既存`.env.example`の項目から渡す。私的ベンチを探索せず、設備のADDRESS/FRAMING/TCP_PEERをモデルへ流用しない。今回の固定寸法は境界を分離する検査fixtureの条件で、恒久設備や実機の宣言値の制約ではない。

常に`full_conformance=false`。実際のhostの送信抑制、silent overflow loss、session要求のpressure下での再送/拒否cache、並行処理、実際のTCP/USB、再起動は未検査。
