# 資源寿命の適合検査

SPEC core §4.2/5.2/6/9を、具体的な資源opと分けて検査する。公開テストは設備や私的ベンチを探索しない。実装に合わせたadapterと、明示した個体・SPEC・通信入口を渡す。通常のcore検査のPASSだけでは、資源寿命のPASSにはならない。

`ResourceChecks(checks, adapter).run()` は14項目の資源契約と、対象fnのinterface宣言検査を含む補助レポートを返す（サンプルは計16項目）。初めにconfirm、個体、core宣言、interface宣言を照合し、失敗したら資源操作を止める。適用する環境は他の利用者が操作しない共有lockで隔離する。実機の資源作成は接続・配線に応じた操作になるため、adapterと設備の許可範囲を先に確定する。コードには特定targetやピンを固定しない。

adapterは次を提供する。符号化と応答の形式検証を担当し、再利用禁止・解放・再送の合否は共通検査が決める。

- `slots`: `(fn, kind)` の組。別fnと同fnの別種類を含む、対象資源の全組を明示する。
- `request(action, sid, slot, rid=0)`: create / close / useの要求bytesを返す。corrは渡されたChecksで採番する。これらはsession必須の操作を選ぶ。
- `decode_created(payload)`: 成功した作成応答を検証し、u16資源IDを返す。
- `decode_empty(payload)`: close / useの成功応答をそのinterfaceの仕様で検証する。名前はサンプルの空応答に由来し、実interfaceの固定項目も検証してよい。
- `snapshot()`: session 0の観測など、再送履歴を押し出さない独立した観測で、全slotの生きたID集合を返す。重複を集合化して隠さず、未知の種類や欠落も検出する。
- `capacity`: テスト設備で再現可能な同時資源数（全slotを同時に保持でき、上限32）。上限まで作成して次の作成がunavailable cause 2になり、部分資源を残さず、既存資源が使えることを確認する。この条件を作れない実装には、この検査セット全体をそのまま適用しない。skipで済ませず未検査の能力として計画に残す。

検査はsession必須、全fn/種類で共通の単調増加ID、閉じた/存在しないID、別fn/種類のcause 6、create/closeの再送、副作用の観測、同IDの新しいopen、open再送、end、自然失効、別IDのforce、容量不足を扱う。自分で取得したsessionのみ操作する。自然失効後はendを送らず、force後は新しいsessionを後始末する。終了後のcreate再送は過去のIDを返しても、資源が復活してはならない。

adapter未提供をPASSにしない。この入口は任意の補助APIで、通常CLIの既存レポートと別に保存する。レポートは常に`full_conformance=false`。購読/通知、route切断、依存順、電気的idle、再起動、65535回の実際の割り当て、未指定fn/種類は未検査と明記する。`wait`を差し替えた仮想時計と実時計の結果も区別する。

## 最小サンプル拡張

`virtual_resource_model.ResourceModel`はin-processで動くサンプル。実機やUSB stackではない。既存`core-v1`と旧target profileを変更せず、現コア実装に明示的な拡張hookを渡す。サンプル名は`io.github.open-embedded-probe.resource`、revision 1、fn 1と2、instance 0と1。OEP標準interfaceではなく、規範registryにも登録しない。

| op | 要求固定部 | 成功応答 | session |
|---|---|---|---|
| 16 create | kind:u8（1または2） | resource:u16 | 必須 |
| 17 close | kind:u8, resource:u16 | 空 | 必須 |
| 18 use | kind:u8, resource:u16 | 空 | 必須 |
| 19 snapshot | 空 | count:u8, count組の(resource:u16, kind:u8) | 0で可 |

整数はlittle endian。要求の後続TLVは現コアの規則で検証する。snapshotは要求fnの全生存資源を返す。全fn/種類で同時8資源。9個目または番号枯渇でunavailable cause 2。snapshot以外のkindが1/2以外ならmalformed。作成以外のIDが未存在ならno_resource、他のfn/種類ならunavailable cause 6。購読は宣言しない。

`conformance_resource_sample.SampleResourceAdapter`は上表を検査側で独立に符号化する。probe実装をimportしない。利用例は`tests/test_conformance_resources.py`。明示したSPECのregistryを読み込んだChecksと組み合わせ、functions=(1, 2)、kinds=(1, 2)、capacity=8を指定する。モデル起動時のbootはOS乱数、テストでのみ固定値を渡す。

モデル単体では再起動とID上限の境界も検査する。内部counterを65535に進める刺激はモデルの境界検査であり、実機で65535個作った証拠にはならない。

最小の実時計実行は、公開Python環境またはArduinoのuv環境から次のAPIを使う。SPECとlockのpathは環境設定から明示的に渡す（`OEP_CONFORMANCE_SPEC`、`OEP_HW_LOCK`）。実機を開かず、モデル内だけで資源を作る。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance_resources import ResourceChecks
from oep_client.conformance_resource_sample import SampleResourceAdapter
from oep_client.virtual_resource_model import ResourceModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = ResourceModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    adapter = SampleResourceAdapter(checks, functions=(1, 2), kinds=(1, 2), capacity=8)
    report = ResourceChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

保存時は新規ファイルを`open('x')`で予約し、SPEC commit/dirty/hashと検査器source hash、モデルsource hash、実行日時をraw交換とともに残す。既存の通常CLI/USBモデルと出力先を分ける。モデルにはADDRESS/FRAMINGの設定を使わず、実機adapterを自動選択しない。
