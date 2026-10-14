# 資源を指定するstreamとdebug connectionの寿命検査

common §1の位置付きstreamと§2のdebug connectionを採用し、streamがconnectionに依存すると明示した拡張へ適用する。`DependencyChecks`は前提1項目＋動作18項目をinterface段階で報告する。全fnの宣言検査と、標準OEP op固有の検査は別途行う。

| 契約 | 検査 |
| --- | --- |
| 明示addressing | 2 connectionと個別streamを作り、要求の資源IDで読み分ける。read/marksはSID 0で使えて非消費。最後に作ったtargetを暗黙に使わない。 |
| global ID | connectionとstreamで1つのID空間。0を割り当てず、同boot内で閉じた番号を使い回さない。 |
| clear | 読みのbufferだけ消しpositionを保持しclear mark（共通 2）を付ける。送信queueを消さない。 |
| stream close | streamとbuffer/queueを解放し、parent connectionは残す。再作成は新ID、position/serial 0、空buffer/marks/queue。 |
| connection close | 依存streamを先にすべて解放する。別connectionとそのstreamは変えない。 |
| 拒否 | 解放済み/未知IDはno_resource、別kindはunavailable cause 6。malformed、外国session、SID 0の変更操作も無変更。 |
| 再送 | stream open/close/read、connection detach、host markの再送は保存済みの結果。閉じたstreamを復活させず、新しいstreamも閉じない。 |
| session | 同じIDのopenは資源を保持。end/lease期限切れ/別IDのforceは依存順に解放し、新sessionへ引き継がない。end/expiry後のread再送は過去のbytesであり、資源の存在を意味しない。 |
| target restart | 明示した論理restart刺激ではconnectionを閉じず、streamの出力/position/queueを保持しconsoleのrestart mark（0x41/havereset 1）を付ける。 |

`SampleDependencyAdapter`は両部品の採用、resource addressing、fn/op対応、容量8資源・送信queue 4 byte、規範参照を明示する。`DependencyModel`は独立した論理fixture。各caseは新bootの明示logical restartから開始する。release eventは独立状態観測で、streamがparentより先に解放される順序を照合する。物理メモリ消去や電気的idleの保証ではない。leaseは実時計の待機または単体用の仮想時計で検査する。

sample contractは次の通り。すべての応答の末尾にはTLVを置ける。名称は独自拡張のもので、標準console/wireの互換実装ではない。

| fn | op | 要求の固定部 | 成功応答の固定部 | lock |
| --- | --- | --- | --- | --- |
| 1 connection | 16 attach | 空 | connection u32 | 必須 |
| 1 | 17 detach / 18 use | connection u32 | 空 | 必須 |
| 1 | 19 list | first u16 | more u8/count u8/count個のconnection u32 | 不要 |
| 2 stream | 16 open | connection u32 | stream u32 | 必須 |
| 2 | 17 read | stream u32/from u8/arg u64/max u16 | common §1.2 | 不要 |
| 2 | 18 marks | stream u32/from_serial u32 | common §1.3 | 不要 |
| 2 | 19 clear / 22 close | stream u32 | 空 | 必須 |
| 2 | 20 mark | stream u32/value u8 | 空 | 必須 |
| 2 | 21 write | stream u32/count u16/data | accepted u16 | 必須 |
| 2 | 23 list | first u16 | more u8/count u8/count個の(stream u32/connection u32) | 不要 |

資源番号はu32で、同じboot_idの間は再利用しない（core §9、共通部品 §1/§2）。

一覧は作成順にページングする。1 parentに複数の独立streamを許すsampleで、consoleのmechanism 0〜2が1 connectionにつき1 streamという制約を実装したものではない。pin割当やtarget選択はこのsampleに含めない。break刺激は論理fixtureによる明示connection解放で、線切れの判断や「失敗応答を送ってから閉じる」順序の検査ではない。

単体23件でparent先行解放、孤立stream、stream closeによるparent解放、clearによるqueue消去、target restartによる出力消去、ID再利用、暗黙の最後のstream、read消費、end後の資源保持、marksの資源誤指定、detachの再実行、必須op宣言欠落、空応答の不正TLVを検出する。採用条件不足、選択SPEC mark値変更、所有中のlogical restart拒否、結果の段階/未検査範囲も確認する。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_dependencies import DependencyChecks
from oep_client.conformance_dependency_sample import SampleDependencyAdapter
from oep_client.virtual_dependency_model import DependencyModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = DependencyModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = DependencyChecks(checks, SampleDependencyAdapter(model)).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示する。私的ベンチへの依存/探索や新しい設備設定は加えない。通常CLIへ自動追加しない。

`full_conformance=false`。物理解放/pin idle、線切れの判断/応答後のclose、標準mechanism制約、target debug操作、通知dataの配送、production Host統合、別容量/並行writerは未検査。SPEC、既存仮想model、Arduino firmwareは変更しない。
