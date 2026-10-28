# Hostの未知値分類

core §2.4の失敗分類と、値を残すenum・無視するflagsを別々に検査する。
readerを明示指定し、SPEC checkoutと出力先も指定する。production Hostへ自動適用しない。

採用する形は標準debug read_blockのdone/status/語、wire scanのtried/count/entry、
core confirmの固定部分とtransport TLV、rejected unavailableのcause TLV。
未知resolution・outcome・reject reasonは失敗で、未知payloadの形を解釈する必要はない。
statusは0だけが成功で、未知statusも失敗である。固定形を読むならdoneと語の並びを勝手に変えない。
unknown failureをValueErrorで知らせるreaderも許容する。

causeやscan entryのkindは、未知でも受け取った整数値を保つ。
confirmはflagsを持たない16 byteの固定部分である。
causeは0x01〜0x3F本体、0x40〜0x7Fは要求先fnのインターフェースが使う。どちらも未知値として整数のまま保つ。
Probeが未知値を送ってよいという規則ではなく、Hostの将来値に対する読出し検査である。
u8の値域をすべて列挙する。reasonは選択registryで定義されない値を刺激する。

decoderのAPIはdecode(frame, shape)で、shapeはdebug/scan/confirm。
返すmappingのsuccessfulと各固定フィールドを照合する。失敗値はValueErrorによる不使用も許容する。
causeとscan kindは整数として表示層へ渡せることを確認する。UIでの実際の表示は別検査である。

```python
import json
from pathlib import Path
from oep_client.conformance_host_values import HostValueChecks
from oep_client.conformance_host_values_sample import decode

report = HostValueChecks(decode, spec="/explicit/path/to/oep-spec").run()
Path("/explicit/output/host-values.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

uv環境でuv run python example.pyとして実行する。
単体はuv run pytest -q tests/test_conformance_host_values.py。
SPEC commit/dirty/registry SHA、raw frame、読出し結果、checker/sample SHAをreportに残す。
独自readerのsource/版は利用者が別途保存する。public runnerはprivateベンチや隣接checkoutを探索しない。

PASSは選んだ形の読出し範囲に限る。他のstatus/reason/flags、全TLV幅、
全payload、production Host、target実行や経路回復は証明しない。full_conformanceは常にfalse。
