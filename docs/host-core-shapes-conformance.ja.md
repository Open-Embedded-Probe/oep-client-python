# コア応答の固定形と既知TLVの読出し検査

`HostCoreShapeChecks(decoder, spec=...)`は第一段階のHost部品検査。
明示したSPECのcore revision 1について、実際の8操作の成功応答、11拒否理由、
33の既知TLV文脈を独立したバイト列と照合する。
`conformance_host_core_sample.decode`は接続例であり、production Hostへ自動で組み込まない。
私的ベンチ、設備設定、兄弟checkoutの探索は不要。

decoderへ渡すのは応答frameと要求したop、describeなら対象のfn。
応答にfn/opが無いため、この文脈は呼出元が明示する。
要求のcountや要求した長さを使って応答の終端を決めない。
結果は`corr`、`successful`、`fixed`、採用した`tlvs`の辞書。
TLV値と名前は生bytesで保持し、表示前のtext sanitizationは別の担当とする。
壊れたframe/payloadはValueErrorで拒否する。
壊れた既知TLVは、その値を使わず無視するreaderも許す。

以下はcore本文をレビューした固定形の台帳。幅はpayloadの固定部分で、要求10 byte/応答5 byteの見出しを含めない。
全行の後ろへ追加できるのはTLVだけ。listのentryは自分の長さを持たず、name_lenだけが名前の長さを決める。

| op | 要求固定部分 | 成功応答固定部分 |
|---|---|---|
| confirm | magic4、min_rev1、max_rev1（6 byte） | magic4、revision1、max_frame2、window4、max_inflight1、boot_id4（16 byte） |
| list | first2 | total2、count1、count × entry。entryはfn2、instance2、revision1、name_len1、name |
| describe | fn2、first2 | more1 |
| clock | 無し | boot_id4、uptime_ns8（12 byte） |
| open | lease_ms4、force1（5 byte） | lease_ms4、boot_id4（8 byte） |
| end | 無し | 無し |
| keepalive | 無し | 無し |
| lock_state | 無し | locked1、remaining_ms4（5 byte） |

省略可能なownerはopen要求とlock_state/locked応答のTLVで、固定部分には入らない。
openのownerは本文§6.4に従って非critical。今回のreader検査は要求encoderの確認ではない。
拒否payloadはlockedだけremaining_ms4、unsupportedだけtag1、ほかの9理由は固定部分無し。
unsupportedのtag 0は固定部分の値を示す印で、TLV番号0とは区別する。
未定義のcore failed/partial payloadを成功の形で読まない。

既知TLVの文脈は次のとおり。

- describeの共通9 tag（setting / setting_choice を含む）をfn 0と非0それぞれで確認する。
  role_channelsのrole/base/bitmap、channel_groupのn×3 byte、opsのbase/bitmap上限、setting と setting_choice の名前の長さを含む。
- fn 0 describeの9 tagはfirmware/model/unit_id/channels/channel_name/netif/transport/chip/max_op_ms。
  非0 fnのtag 0x40をfn 0のfirmwareとして読まない。
- confirmのtransport、locked/lock_stateそれぞれのowner、unavailableのcause/channel/item/fn。
- unsupportedはconfirmでsupported/channel/item/fn、ほかのopでchannel/item/fn。
  supportedのtag 1をcauseやownerとして読まない。
  インターフェースのindexはcoreのtagでなく各インターフェースの0x40〜0x7Fにあるため、
  registryのcore `unsupported_payload`にindexが無いことも照合する。
- 設定の op（get〜network）は番号だけを照合し、結果の形はこの検査で読まない。

固定形、短い見出し/固定部分/数付き名前、後続TLV破損、既知値の幅/count、
重複の最初の値と繰返し順序、文脈の分離、可変長境界、boolを確認する。
KNOWN-WIDTHは、正しい既知TLVの後に同じtagの壊れた値を置き、後の値も検証して使わないことを確かめる
（core §2.3。応答全体を使わないreader、壊れた値だけまたはそのtagを捨てるreaderを許す）。
重複の2番目には異なる有効値を入れる。既知tagのhigh-bit aliasも未知tagとして読み飛ばす。
知らないTLVでも、長さが末尾を超えていれば壊れている。
listは0/1/48/255 entry、channel_groupは0/1/255 member、text/TLV値は65535 byteまでの試料を含む。
これらはpayload部品の境界検査で、transportのmax_frameへ収まることを証明するものではない。

本文SHAとprotocol revision/番号/tag空間をレビュー時の値に固定する。現在の固定はoep-spec 02c08c9のcore本文。
SPECが変わった場合は入力を読み直すまで停止し、番号が同じだけで形の互換性を推定しない。
固定形を変更する将来のrevisionは、このrevision 1 readerへ無条件で適用しない。

単体103件は正常report1、固定形の誤り19、既知値の誤り30、後続重複の壊れた値の使用28、意味的故障18、
壊れた既知値の非使用を許すreader1、SPEC変更の検出5、core tagとしてのindexの検出1。
要素長の誤った要求、短い形の受理、critical alias使用、文脈を跨ぐcache、最後の重複値、
繰返しのsort、boolを0/1だけで読む誤りなどを捕捉する。

```python
from oep_client.conformance_host_core import HostCoreShapeChecks
from oep_client.conformance_host_core_sample import decode

report = HostCoreShapeChecks(decode, spec="/explicit/oep-spec").run()
assert report["status"] == "passed"
```

```sh
uv run pytest -q tests/test_conformance_host_core.py tests/test_conformance_host_tlv.py
```

`full_conformance`はfalse。全インターフェースの形、要求生成/critical方針、Probe送出、
複数ページの集約・必須宣言の有無・metadataの意味、corr/再送/回復/通知、
production Host・物理動作はこの検査の対象外。
[一般TLV reader](host-tlv-conformance.ja.md)と[未知値分類](host-value-conformance.ja.md)も別reportとして保持する。
