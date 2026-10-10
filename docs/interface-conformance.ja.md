# 全インターフェイスに使う宣言の準拠検査

`InterfaceChecks`は標準・独自インターフェイスに同じ検査を適用する。OEP固有opの期待値や私的設備を参照しない。明示したSPEC registryとwire交換だけを受け取り、結果を`interface`段階で報告する。

前提1項目に加え、各fnへ8項目を実行する。今回の独自sampleは3 fnで25項目。掲載順は9/2/5とし、fn 2/9は同じ名前・revision 1、fn 5は同じ名前・revision 2を使う。

| case | 規範・観測 |
| --- | --- |
| NAME | core §13 規則1。2つ以上の非空label。先頭/末尾のhyphenを禁止。文字集合・1〜48 byteはlistの検査で確認。 |
| OPS | §1.2/7.4/12。ops必須、bitmapの上限、共通予約番号禁止、subscribe/unsubscribeの対。 |
| FIXED | §2.3/7.4。max/min_clock_hz・featuresはu32、max_lengthはu16。値域や意味を推測しない。 |
| CHANNELS | §7.4/7.5。role_channelsのheaderと繰り返しの和集合、channel_groupのcount/固定要素、宣言channel数の範囲。 |
| PAGES | §7.3。各TLV位置からの応答を全体のsuffixと照合。more、空の末尾、65535、TLVがframeに収まること。 |
| STABLE | §7.2/7.3。同bootのlist不変性、sessionなし・保持中・end後のdescribe不変性。 |
| ABSENT-OPS | §1.2/4.3。opsにない0〜255の要求をSID 0で送り、session_requiredなどより先にunknown_operationで拒否する。 |
| INSTANCE | §7.2。同じ(name, revision)をfn昇順で0から番号付け。掲載順や別revisionとは分ける。 |

共通タグの意味を読む際は、非反復タグの最初の値を使う。未知非criticalタグを解釈せず保持し、ページ位置の計算にも含める。role_channelsは立ったbitのchannelだけを検査する。未定義のrole値、clockの大小関係、group番号の意味、標準opの必須集合を汎用検査から推測しない。

位置付きストリーム、debug connection、statusなどの[common部品](../../oep-spec/interfaces/oep-if-common.ja.md)は、インターフェイスがその部品を採用すると定めた場合だけ適用する。全fnへread/marks/attachを要求しない。資源・購読・通知の寿命は既存の明示adapter検査を併用し、固有opの副作用・配線・電気的観測は3段階目で検査する。

単体29件で名前、固定幅、channel範囲/count、ops、位置指定だけ壊れたページ、保持中だけ変わる宣言、未宣言opの拒否優先順位、不正instance、ops欠落を検出する。48 byteの名前、標準名、独自名、未知タグ、反復タグ、異なるrevisionも確認する。fnがない端点では各fnの検査を適用外と記録し、全準拠とはしない。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_interfaces import InterfaceChecks
from oep_client.virtual_declaration_model import DeclarationModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = DeclarationModel()
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    report = InterfaceChecks(checks).run()
assert report['status'] == 'passed', report
```

これは明示した仮想sampleの例。実機へ適用する場合は、wire交換・個体・SPEC・共有lockを明示する。通常CLIへ自動追加せず、`.env.example`に新設定を追加しない。公開側から私的ベンチや隣接checkoutを探索しない。

`full_conformance=false`。宣言されているopの実行、標準インターフェイスの必須op、共通部品の動作、資源/購読/通知、ピン選択の実効制約、firmware版を越える不変性、別経路の最小frame、並行writerは未検査。今回追加した独自modelは宣言のfixtureで、channelの空き状態やピン操作まで実装したprobeではない。SPEC・既存の仮想model・Arduino firmwareは変更していない。
