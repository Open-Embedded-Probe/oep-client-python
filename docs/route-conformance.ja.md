# 通知経路・応答優先・送りかけqueueの検査

SPEC core §11.4、transports §3の契約を、2つの明示経路とinstrumentationを持つモデルで検査する。受信済み要求、通知生成、送信停止/部分送信を制御し、各経路の全raw messageと未送信suffixを観測する。設備の自動探索や私的ベンチ依存は加えない。

共通契約は、応答が要求元に返る、通知がsubscribe元だけへ届く、受信済み要求の応答を通知より先に送る、送りかけ通知の残りbytes合計がmax_frame×2以内、送信停止中も他経路の処理が進む、probe内部で捨てた通知もdata/event共通seqを消費する、経路切断だけではsession/資源/購読/再送履歴を解放しないこと。

最小サンプルは `io.github.open-embedded-probe.routes` revision 1、fn 1/2。資源/購読/data/eventはstreamサンプルと同じ。primary/peerという2つの論理接続を持つ。session Sの要求はprimaryだけ、peerはsession 0の照会に使う。primary切断後はpeerで別session Tをforceし、Tだけをpeerでendする。Sを新しい経路で再送しない。

モデルは1経路につき1書き手。部分送信済みmessageは完了させてから次のmessageを送り、残りの受信済み要求への応答を、新しい通知より先に選ぶ。通知の生成時にseqを消費し、閉じた経路やqueueに入らない通知を捨てる。queue上限は未送信のOEP message bytesで検査する。COBS/CRC/lengthなどのtransport framing overhead、実際のOS/USB/TCP bufferと非同期競合はこの段階に含めない。

queue残量・購読・再送履歴の保持はinstrumentationで観測する。これは実機の公開opを新設する仕様ではない。観測能力のない実機にこの検査をそのまま適用しない。実機wireの証拠、経路ごとのwindow/max_inflight、実際のTCP再接続は別の検査として残す。レポートは常に部分適合。

## adapterと証拠

`RouteChecks(checks, adapter).run()`はprimaryのidentity/宣言＋6動作契約、計9項目を返す。peerの個体/boot/宣言もidentity内で確認する。adapterはprimary/peerという明示した異なる経路、functions、exchange、hold(route, byte_budget)、submit([(route, request)]), service, receive(route), queue(route), stimulate, feed, close, state, create_resourceを提供する。`trace`には各制御の引数・結果・raw bytes・時刻・errorを保存する。

`queue`は待機中通知と、部分送信通知の未送信suffixをbytesで返し、共通検査が合計する。`state`はholder、資源、購読seq/route、保持した要求/応答を観測する。これらはinstrumentationが必要で、通常の実機公開opから推定した値を代用しない。`feed`は外部入力のpositionを返す。`create_resource`は明示したinterfaceの資源opを符号化・検証する。

`virtual_route_model.RouteModel`はstreamモデルの生成処理を使い、通知queueと単一writerを追加する。論理primary/peerは同じTCP listenerの別接続として扱う（socket/framingは未実装）。`conformance_route_sample.SampleRouteAdapter`はモデルをimportせず、明示して渡された観測対象を呼ぶ。

実行例は公開Python環境またはArduinoのuv環境から、SPEC/既存共有lockを明示して使う。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance_routes import RouteChecks
from oep_client.conformance_route_sample import SampleRouteAdapter
from oep_client.virtual_route_model import RouteModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = RouteModel()
    adapter = SampleRouteAdapter(model)
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-routes-1')
    report = RouteChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

新規JSONへSPEC/検査器/モデルhash、raw交換とadapter traceを保存する。既存`.env.example`のSPEC/共有lockを使い、ADDRESS/FRAMING/TCP_PEERをモデルへ流用しない。公開テストから私的ベンチを参照しない。

部分送信ではmax_frameの通知を7 byte進め、残りsuffixを独立に照合する。次のmax_frame通知を追加すると残量はmax_frame×2−7。10 byteのeventは入らず、seqを消費して捨てる。既に始めた通知を完了し、clock応答、次の通知の順で出力する。drop検査では32個のdata/eventを交互に生成し、保持された通知の内容/位置/seqと、その後のseq 32を確認する。

切断検査は最後に実行する。閉じたprimaryでSを後始末せず、peerで自分のSを別ID TでforceしTをendする。takeover失敗時もSを別経路で再送しない。実機adapterでは個体/boot/owner再確認とleaseによる失効を含む計画が別途必要。

`service_budget_ms`（サンプル100ms）は、送信停止中のserviceが戻ることを判定する設備の観測予算。1〜1000msを明示し、超過はこの実行のFAILとする。OEPのmax_op_msを変更する規範ではない。takeover直前にpeerのbootとinstrumentationのholderが自分のSのままか再確認し、不一致ではforceしない。
