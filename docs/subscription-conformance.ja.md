# 購読・出来事の寿命検査

SPEC core §6/9/11から、購読と出来事の契約を共通検査にする。購読opの符号化と合否は共通検査が担当する。具体的な出来事を発生させ、そのpayloadを検証する方法は明示adapterが担当する。標準OEP interfaceの移行、データのまとめ送り、物理USB/TCPの検証とは分ける。

最初の適用条件は、2つ以上の通知fnに対して、識別できる出来事を外部から1回ずつ発生させられること。adapterはfnの一覧、刺激、出来事payloadの検証、指定した観測時間内の通知受信を提供する。要求/応答の経路を変更しない。自動探索、私的ベンチ依存、実機adapterの自動選択は行わない。

共通検査は購読なし、session必須、他sessionの購読操作拒否、fn間分離、購読の置換とseqリセット、subscribe再送でseq維持、unsubscribeの冪等性と再送、同ID openとopen再送で購読維持、end/自然失効/forceで停止、不正subscribeが前の購読を壊さないこと、データ用閾値が出来事に掛からないことを扱う。通知のrole/fn/seq/長さとpayloadを別々に検証し、raw通知と刺激・観測の時刻を保存する。

次の範囲はこの段階のPASSには含めない：data/event共通seq、seq一周、dataのmin_bytes/max_delay、応答優先、通知経路、route切断、max_frame×2の送信queue上限、dropによるseqの欠落、実機の非同期処理。無通知のPASSは指定した観測窓に対する結果であり、無期限の保証ではない。

## サンプル拡張の契約

名前は `io.github.open-embedded-probe.notify`、revision 1、fn 1/2、instance 0/1。OEP標準のinterfaceではない。資源サンプルと同じop 16〜19、全fn合計8資源に、必須op 1 subscribe / 2 unsubscribeを追加する。subscribe固定部はmin_bytes:u16、max_delay_ms:u32、成功応答は空。unsubscribe固定部は空、成功応答は空。両方session必須。要求後続TLVはコアの規則に従う。未知critical TLVと短い固定部は副作用なく拒否する。

出来事kind 1の固定部はmarker:u32、後ろはTLV。markerは刺激の識別子で、target/ピンの番号ではない。外部刺激に対して購読中のfnだけがrole 3を出す。seqはsubscribeごとに0から、同fnで増える。出来事はデータ用閾値を待たない。購読の無い刺激は通知を出さない。data通知は実装しない。固有status/reject、describeタグ、pin role、外部仕様、チップ固有手順は持たない。資源と購読はコアのsession寿命に従い、bootを跨がない。

## adapterと実行

`SubscriptionChecks(checks, adapter, wait=time.sleep).run()`は、identity確認＋15動作契約と対象fnの宣言検査を返す。2 fnのサンプルは計18項目。最初に個体、core/IF宣言、adapter能力を照合し、失敗したら購読操作と刺激を止める。transport/stimulus/観測のIO失敗後は後続をBLOCKEDにする。規約違反はFAIL、明示設定不備をskipにしない。`full_conformance`は常にfalse。

adapterの能力は以下のとおり。

- `functions`：明示した通知fnを2〜8個。これはboundedな検査設備の条件で、OEP規範のfn数制限ではない。
- `observation_ms`：1〜100ms。この検査セットで出来事が届く設備の観測窓。設備がこの条件を満たせない場合は別の窓/刺激計画を作り、今回の検査を適合証拠にしない。
- `stimulate(fn, marker)`：指定fnの出来事を1回発生させる。markerは検査側が採番したu32。これはOEP要求ではなく、外部刺激であり、sessionの経路や再送履歴を変更しない。
- `receive(timeout_ms)`：指定した時間の観測を行い、その窓に受信した全通知フレームをbytesのlist/tupleで返す。未知/破損/重複通知をhost実装で捨ててはいけない。
- `decode_event(frame)`：interface固有kind/固定部/後続TLVを検証してmarkerを返す。共通ヘッダのrole/fn/seq/長さと比較の合否は共通検査が担当する。今回のadapterは出来事のみを刺激するためdataの受信も期待値違反となる。

`conformance_subscription_sample.SampleSubscriptionAdapter`は上のサンプル契約を独立に復号する。probe実装をimportしない。`virtual_notification_model.NotificationModel`は出来事と資源を持つin-processモデル。`.emit()`と`.drain()`は外部刺激/観測の入口であり、物理transportの応答優先や送信bufferを模擬しない。既存core-v1、資源だけのサンプル、旧target profileと分けて使う。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance_subscriptions import SubscriptionChecks
from oep_client.conformance_subscription_sample import SampleSubscriptionAdapter
from oep_client.virtual_notification_model import NotificationModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = NotificationModel()
    checks = Checks(model.handle, registry, 'virtual-notify-1')
    adapter = SampleSubscriptionAdapter(model, functions=(1, 2), observation_ms=50)
    report = SubscriptionChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

Arduinoのuv/pytest CLI plugin環境でも同じAPIを使う。SPEC/既存共有lockは明示pathを渡す。既存`.env.example`のSPEC/lock項目を使い、新しい設備設定は加えない。ADDRESS/FRAMING/TCP_PEERはこのモデルに使わない。JSONは新規pathを`open('x')`で予約し、SPEC/検査器/モデルsource hash、時刻、raw要求/応答とraw通知を保存する。公開テストから私的ベンチを参照しない。

pytestでは`wait`を仮想時計に差し替えた単体検査も行う。実時計の記録と区別する。モデル単体の再起動検査はコア/資源/購読の履歴がbootを跨がないことを検証するが、公開補助レポートの実時計18項目には再起動を含めない。既存コア49項目と資源16項目も同じ通知モデルへ適用して回帰を確認する。
