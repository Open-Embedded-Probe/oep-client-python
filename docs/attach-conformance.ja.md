# 標準wire attachの事前検査とconsole open

`AttachChecks`は第三段階`oep-interface`として、選択SPECに基づくwire attachの要求と拒否、console openの制約を検査する。前提1項目＋動作21項目の22項目。コアと共通部品の準拠は別の検査を併用する。

| 契約 | 検査 |
| --- | --- |
| 必須TLV | max_speed/pinsがない要求はmalformed。既知値の長さが違う場合もmalformed。 |
| critical | hostの要求生成は速度とpinsにcriticalを付ける。probeは既知TLVをbitの有無によらず同じに扱う。未知criticalはunsupportedで受信したraw tagを返し、未知noncriticalは無視する。 |
| 同じtag | critical bitを除いた同一tagは先頭を使う。後の速度やpinsで置き換えない。 |
| 事前検査 | min_clock_hz未満の速度、宣言外pin組、SWIOのswclk != 0xFFFF、未対応methodを拒否する。拒否前後のallocator/資源/buffer/論理targetアクセス記録を照合する。 |
| 競合 | 使用中pinはunavailable cause 1、wireの接続数上限はcause 2。既存接続へ暗黙に加わらず、空きを作るために閉じない。 |
| attach応答 | connection/id/flags/speedの固定11 byte＋TLV。speedは要求上限以下。connections一覧の可変tid/末尾TLVも読み、fn/pins/speed/IDを独立状態に照合する。 |
| 立ち上げ失敗 | completed failed / status line＋TLV。新しいlive資源を残さず、既存connection/streamの出力を変えない。 |
| console open | connection/mechanismが固定部。stream/flags 0＋TLVを返す。宣言外/未知/preset noneの方式はunsupported payload 0。 |
| console資源 | 不明connectionはno_resource、streamをparentにする要求やARM/SWD connectionはunavailable cause 6。 |
| 方式の競合 | mechanism 0〜2は1 connectionに1 stream。既存streamと同じ方式、別の方式のどちらのopenもcause 6。 |
| 複数target | 別connectionには個別streamを作り、streamsの作成順・connection・mechanismを照合する。SID 0のreadは個別出力を読み、消費しない。 |
| 再送 | detach/close後のattach/open再送は過去のID/応答を返すだけで、立ち上げ・資源作成を繰り返さない。作り直したstreamは新IDと空の状態。 |

`SampleAttachAdapter`は標準名/fn、論理pin候補、方式の宣言subset、規範参照を明示する。`AttachModel`は物理targetを持たない独立fixture。fn 1 RVSWD、2 SWIO、3 SWD、4 console、8 logical channels、各wire最大2 connection、全体8資源、min_clock_hz 1000、consoleはSDI/DMDATAを宣言する。未宣言のdmseqを拒否する条件も検査する。

論理pin組はRVSWD (0,1)/(2,3)/(6,7)、SWIO (0,0xFFFF)/(2,0xFFFF)/(6,0xFFFF)、SWD (4,5)。channel_groupで明示する。scanは実装しない。method 0の論理成功だけを確認し、haltや電気的な速度選定を証明しない。reset/idle_clock/targetselの動作は未検査。reset能力がないsampleへのcritical resetの拒否は確認する。

仮想コアへ`request_tags` / `dispatch_request`の明示拡張入口を加え、拡張固有critical TLVを検査・実行へ渡す。入口がない既存拡張は従来の処理を使う。session・header・cacheの判定は入口より先に行う。同一要求再送でTLV解析や実行を繰り返さない。

単体18件で必須TLV無視、未知critical無視、既知noncritical拒否、raw tag喪失、後のtag優先、拒否時targetアクセス、console flags不正、ARM console受理、方式競合無視、速度上限超過、再送再実行、connectionsのmetadata不正を検出する。採用条件/選択SPEC変更、所有中fixture再起動拒否、拡張入口とsession/cacheの優先順も確認する。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_attach import AttachChecks
from oep_client.conformance_attach_sample import SampleAttachAdapter
from oep_client.virtual_attach_model import AttachModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = AttachModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = AttachChecks(checks, SampleAttachAdapter(model)).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示し、私的ベンチへの依存/探索や新しい設備設定を加えない。通常CLIへ自動追加しない。

`full_conformance=false`。物理bringup/scratch/速度/pin idle、scan、reset/idle_clock/targetsel動作、targetインターフェイスの操作、console mailbox/framing/配送、SDIのcount=0 write、通知、別容量/並行要求、production Host統合は未検査。SPEC・Arduino firmwareは変更しない。
