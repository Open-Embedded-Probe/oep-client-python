# linkの必須source/sinkを検査する

LinkTransferChecksは選択した`oep.probe.link` revision1のsource/sinkをraw要求・応答と保護対象の状態で検査する。
link §2のデータpattern、経路max_frameによる長さ、任意sinkデータ、TLV、拒否時不変、ロック不要、再送を扱う。
port_speedを持たない適合構成を明示して採用し、port_speedの要求はunknown_operationと確認する。
port_speedを持つ構成の握手・応答後切替・verify/idle/session期限は別の検査が必要である。

## 仮想モデルと観測

LinkTransferModelは現在のCoreとGPIO/planモデルを組み合わせる。
fn1はplan、fn2/3はGPIO、fn4はlink。経路max_frameは64、256、1024の3採用構成。
検査ごとに明示的に論理再起動し、新しいboot_idをconfirmで読み返す。
GPIO fn2 role1へchannel1をplanし、高出力にする。plan・pin/idle・駆動journal・coreの所有権を
実際のモデルからsnapshotし、転送/拒否/再送の前後で一致を要求する。
leaseの更新時刻と再送cacheはcoreの正当な変更なので、この不変条件の対象にしない。
reportのstate_observationsへseedと要求前後のsnapshotを保存する（各構成68観測）。
seedが実際にplan・出力・所有権を作ったことも検査し、空のsnapshotだけでは通さない。

共通インターフェース宣言はInterfaceChecks、必須opと任意opはRegisteredOpsChecksで先に確認する。
source/sinkの成功・拒否は共通のChecksでresolution/detail/corrを検査し、その後に固有payloadを読む。
これらのPASSはcore全規範の証明ではない。public checkerはprivateベンチや隣接SPECを探索しない。

```python
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_interfaces import InterfaceChecks
from oep_client.conformance_link_transfers import LinkTransferChecks
from oep_client.conformance_link_transfer_sample import SampleLinkTransferAdapter
from oep_client.virtual_link_transfer_model import LinkTransferModel

registry, identity = spec_identity("/explicit/path/to/oep-spec")
model = LinkTransferModel(lambda: 0, boot_id=17, max_frame=1024)
checks = Checks(model.handle, registry, "virtual-resource-1")
common = InterfaceChecks(checks).run()
specific = LinkTransferChecks(checks, SampleLinkTransferAdapter(model)).run()
assert common["status"] == specific["status"] == "passed"
# 保存先を明示し、identity、report、checks.traceとsource/adapterの版を保存する。
```

## 検査項目

sourceはlength 0/1、max_frame−8/−7/−6、u32最大を要求する。
応答lenはmin(length,max_frame−7)、byte kはk&255、応答全体は経路max_frame以内。
1024構成では255から0へのpatternの繰返しも確認する。
sourceの固定値の後ろと、sinkのcount byteの後ろだけが応答/要求TLVの境界である。

sinkはcount 0/1、max_frame−13/−12で、全0・全255・counterの任意データを送る。
要求全体はmax_frame内に収め、応答は固定部分無しの成功（後続TLVを許す）。
短い固定部分・count不足、未知optionalの無視、未知criticalの受取tag echo、
番号0、途中で切れた既知でないTLVも、coreの規則と拒否前後の状態で照合する。

ロック保持中もSID0で転送でき、保持者SIDでも使える。別SIDはlocked、end後の旧SIDはno_session。
end後もSID0のsource/sinkが使える。再送は同じ要求のbyte一致、変更した同corrのmalformedと不変を確認する。
再送用刺激はsource16 byteとsink3 byte。fixtureはrequest/resultを72 byteまで保存する。
adapterは実際の保存上限を返し、23 byte未満なら採用前提不足として停止する。
大きな応答の再送を成功と決めつけない。保存できない応答のresult_lostはcoreの正当な拒否である。

単体は`uv run pytest -q tests/test_conformance_link_transfers.py`。
50件は正常3、共通宣言3、wire故障21、意味的故障18、seed不足1、control不足3、保存前提不足1。
意味的故障は長さ/限界/pattern/空データ、pin/plan変化、短い固定/countの誤受理、
未知critical/壊れたTLVの無視、tag印の消失、誤ったSID受理、再送payload変化、変更要求の誤受理、
未宣言port_speed受理を検出する。各profileの第三段階reportは10項目でfull_conformance=false。

物理転送量・遅延、複数経路それぞれのmax_frame、transport framing、port_speed、
真の並行性とGPIO/plan以外の資源不変、production Host/Probe適合は未証明。
外部adapterが返す状態の出所・版は別に記録する。定数snapshotを実装状態の証拠にしない。
