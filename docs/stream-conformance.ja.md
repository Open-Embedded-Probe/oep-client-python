# 採用を明示した位置付きストリームのread/marks検査

共通部品は、各インターフェイスの仕様が採用すると定めた場合だけ適用する。名前やop番号から自動推測しない。

| 機能 | 適用 |
| --- | --- |
| oep.fixture.uart | common §1を採用。fnで直接指し、同bootのposition/mark serialを戻さない。 |
| oep.target.console | common §1を採用。stream資源を指すため、資源の作成・解放も別途検査する。 |
| oep.fixture.logic / analog | capture文書の区画・世代付き形式。今回のread/marks検査を適用しない。 |
| 独自拡張 | 拡張の仕様とadapterがcommon §1の採用・addressing・op対応を明示した場合だけ適用。 |

`StreamChecks`は前提1項目＋動作14項目の15項目をinterface段階で報告する。今回のadapterは独自のpersistent-fn sampleへ、read/marks/clear/mark/writeの対応、規範の参照、byte ring 64、mark ring 5を明示する。5 opの宣言は必須で、writeがないものをreadだけの成功で適合としない。writeの動作適合は今回の実行範囲に含めない。

| case | 検査 |
| --- | --- |
| EMPTY | 空のread全from、空のmark ring。 |
| POSITION | u64の32bit超の位置、max=0/小さいmax、未来位置の空応答、今からの読み出し。 |
| FRAME | frame上限までの分割、more、全bytesの復元、繰り返しreadの非消費性。 |
| GAP | byte ringあふれ、失った位置差、oldest/now、lost mark。 |
| SELECTORS | kind指定/任意kindの最後のmark、存在しないkind、target resetイベント後のbytes保持。 |
| PAGES | inclusiveなserial指定、22 byte固定要素、more、同じ位置の複数mark、nextの空応答。 |
| WRAP | 新bootの初期serialを0xFFFFFFFEにし、実際にmarkを生成して0を越えるページングを確認。 |
| EVICTION | 押し出された/未知/未来serialは最古へ、保持serialはinclusive、nextは空。 |
| MARK-EVICTED | 指定kindのmarkがリングから消えた場合はnowへ。bytesは保持する。 |
| MARK-GAP | mark位置のbytesだけ失われた場合はgap付きで最古のbytesへ。 |
| LIMIT | u64上限直前の位置を読み、折り返しがないこと。 |
| CLEAR | bytesだけ消去しpositionを保持、clear mark、同一要求再送でmarkを重複しない。 |
| PERSISTENT | host markのdetailと再送、end/新sessionでposition/serial/bytesを保持。 |
| REFUSALS | clear/mark/writeはlock必須。read/marksの短さ、未知from、拒否後の無変更。 |

read/marksはSID 0で使い、各応答の前後で独立stream状態を照合して非消費性を確認する。未知応答TLVを固定部/data/mark配列の後で処理する。markのserial/kind/position/detailは既知の刺激と照合し、time_nsは刺激を挟むwire clockの区間と単調性で確認する。

sample contractはfn 1、read 16（from u8/arg u64/max u16）、marks 17（from_serial u32）、clear 18、mark 19（value u8）、write 20（count u16/data）。read/marksだけがlock不要。writeは今回の論理modelで全入力をqueueへ受け付けるが、物理配送・送信容量・partial/failedを証明しない。

`StreamModel`は独立した論理fixture。外部bytesとmark刺激をadapterで与える。境界値の初期化は毎回新しいbootの明示logical restartとして記録する。同bootのposition/serialを巻き戻して境界を作らない。serialの一周は初期値を境界直前に置く条件で、2^32回の刺激や実機の再起動を行ったという意味ではない。resetイベントはmark刺激であり、実際のtarget resetではない。

単体22件でread消費、gap/more/未来位置、exclusive marks、wrapの数値sort、end時のserial初期化、clear時のposition初期化、reset時のbytes消去、時刻/種別/serialの不正を検出する。選択SPECのenum変更、採用条件・addressing・geometry・reset scope不足、write宣言欠落、所有中のfixture reset、不正seedも確認する。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_stream import StreamChecks
from oep_client.conformance_stream_sample import SampleStreamAdapter
from oep_client.virtual_stream_model import StreamModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = StreamModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = StreamChecks(checks, SampleStreamAdapter(model)).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示する。私的ベンチへの依存・探索や新設備設定を加えない。通常CLIへ自動追加しない。

`full_conformance=false`。資源を指すstreamの寿命、writeの配送/partial/failed、通知data、physical UART/target reset、debug status/done部品、別のring/frame条件、並行producerは未検査。OEP固有opの動作、SPEC、既存仮想model、Arduino firmwareは変更しない。
