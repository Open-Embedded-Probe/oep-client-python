# 標準wire／console固有の宣言検査

`OepDeclarationChecks`は第三段階の`oep-interface`として、選択した標準名・revision 1の必須opと固有describe宣言を検査する。第一段階のコア準拠、第二段階の全fnに使える`InterfaceChecks`・採用した共通部品の検査とは、独立した結果にする。

fnと標準名の対応を明示する。名称による全fnへの自動適用や設備探索は行わない。選択SPECのregistryからop・tag・enumを読む。今回はRVSWD／SWIO／SWD／consoleの4 fnを選ぶ。

| 検査 | 規範と適用 |
| --- | --- |
| fn/name/revision | 選択したfnの標準名とrevision 1がlistに存在し、選択SPECもrevision 1を定める。 |
| wire ops | attach／detach／connectionsが必須。scanは任意なので、ない場合を欠陥にしない。 |
| console ops | open／read／marks／clear／mark／write／close／streamsの8 opが必須。 |
| max_connections | 宣言時はu8。省略時は1として記録。実際の同時接続容量は別途動作検査する。 |
| wire pins | channel_groupまたはrole_channelsから使える組を明示。RVSWD／SWDは役1・2の異なるchannel、SWIOは役1を持つ。範囲外channelは拒否。groupの電気的利用や全候補の探索は証明しない。 |
| SWIO clock | 固定bit時間をmin_clock_hzの正のu32で宣言する。実際の線の周期は未検査。 |
| console mechanisms | tag 0x40を必ず宣言。標準mechanismだけを載せ、presetのnone=0xFFを開ける方式と混同しない。空リストを禁じる規範はないため、非空という条件は追加しない。 |

各項目の検査はSID 0のconfirm/list/describeだけを使う。セッションを取得せず、target操作も行わない。標準名を名乗っただけでop動作の適合とはしない。選択しないfnの標準固有適合も報告しない。

`OepDeclarationModel`は標準名の宣言だけを持つ独立fixture。3 wireはscanなし、RVSWDはmax_connections 2、他2 wireは省略、SWIOは固定clock、consoleはDMDATAだけを宣言する。role_channelsとchannel_groupの両形式を使う。attachやconsoleの配送は実装していない。仮想fixtureが宣言を返す検査であり、稼働probeの証拠ではない。

単体23件で必須op欠落、mechanisms欠落/未知値/preset none、max_connectionsの幅、ピン宣言欠落/同一ピン/範囲外、SWIO clock欠落/幅/0、選択fn/標準名/revision不一致を検出する。scanなしと空mechanismsを許容し、選択SPECに追加された必須opを無視しない。不正な選択は失敗として報告し、第三段階の各結果とSID 0の要求を確認する。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_oep_declarations import OepDeclarationChecks
from oep_client.virtual_oep_declaration_model import OepDeclarationModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
selection = {1: 'oep.wire.rvswd', 2: 'oep.wire.swio',
             3: 'oep.wire.swd', 4: 'oep.target.console'}
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = OepDeclarationModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = OepDeclarationChecks(checks, selection).run()
assert report['status'] == 'passed', report
```

通常CLIへ自動追加しない。既存`.env.example`のSPEC/共有lockを明示し、公開検査は私的ベンチへ依存・探索しない。

`full_conformance=false`。対応するtarget interfaceの存在、要求のcritical TLVと拒否条件、attach/scanの電気的手順、consoleのmechanism競合/配送/framing、共通stream/status/資源寿命、実機ピン、production Host統合は別途検査する。SPEC、既存仮想コア/モデル、Arduino firmwareは変更しない。
