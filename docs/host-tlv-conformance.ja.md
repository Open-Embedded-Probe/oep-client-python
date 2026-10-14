# HostのTLV読出し部品検査

core §2.2/2.3のHost側を検査する。Probeの送出適合とは別の検査で、production Hostへ自動適用しない。
自分のreaderを明示アダプタで接続する。private設備台帳や隣接SPECの自動探索は行わない。

例のpayloadは`value(u16)、blob長(u16)、blob、TLV並び`。
tag 1は任意u8で最初の値を使い、tag 3は繰り返すu8の順序を保つ。
これは拡張の採用例であり、実在のOEP操作へ新しい形や番号を追加する仕様ではない。
decoderは`fixed`（整数）、`blob`（bytes）、`first`（整数またはNone）、`repeated`（整数list）を返す。
壊れたpayloadはValueErrorで拒否する。壊れた既知TLVは、その値を使わず無視する実装も許容する。

検査は未知tag（127も含む）、critical bit付きの既知/未知tag、最初の重複値、繰返し順序、
短い固定部分・数付きdata・TLV、tag番号0、既知値の幅、u16長の境界を確認する。
既知値の幅では、正しい値の後に同じtagの壊れた値を置き、後の値も検証して使わないことを確かめる
（core §2.3、oep-spec a09fa45で明記。payloadを使わない、壊れた値やそのtagを捨てる実装を許す）。
未知tagでもlenがmessage末尾を超えれば破損である。critical bitを消して既知tagとして読む実装は拒否する。
Probeがcritical bit付き応答を送ってよいという規則ではない。

明示したSPEC checkoutと出力先を使う例:

```python
import json
from pathlib import Path
from oep_client.conformance_host_tlv import HostTlvChecks
from oep_client.conformance_host_tlv_sample import decode

report = HostTlvChecks(decode, spec="/explicit/path/to/oep-spec").run()
Path("/explicit/output/host-tlv.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

uv環境で`uv run python example.py`として実行する。部品の単体検査は
`uv run pytest -q tests/test_conformance_host_tlv.py`。
単体はSPEC identityを注入し、上の利用例は実checkoutのcommit・dirty・registry SHAを記録する。
実行reportはraw payload、読出し結果、判定、checker/sample source SHAを保存する。
独自decoderのsourceと版は利用者が別途記録する。

長さ65535の試料はtransportを通さない字句・payload検査であり、frame上限内に収まることは証明しない。
他の固定形・TLV定義、応答corr、通知demux、未知kindとseq、連続通知中の期限処理、
production reader全体はこのPASSに含まれない。reportのfull_conformanceは常にfalse。

実際のcore revision1の8操作・拒否payload・既知TLVは[core形の検査](host-core-shapes-conformance.ja.md)で別途確認する。
