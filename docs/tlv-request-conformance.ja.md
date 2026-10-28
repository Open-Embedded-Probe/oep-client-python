# TLV要求の文脈・幅・critical検査

core §2.2/2.3を明示した仮想拡張で検査する。Coreの実際のsession・再送・拒否経路を使う。
OEP固有操作やproduction Probeへの実装追従とは別で、物理pinを持たない。

fn 1/2はio.github.open-embedded-probe.tlv-requestを宣言し、op16/17を持つ。
要求は固定部分無しのTLV並びで、tag 1は任意。省略時は各文脈のdefaultを使う。

| fn / op | tag 1の形 | default |
|---|---|---|
| 1 / 16 | u8 | 0 |
| 1 / 17 | u16 | 0 |
| 2 / 16 | count(u8)、count byte。count上限48 | count 0 |
| 2 / 17 | enum u8。0は禁止、1/2は定義済み、3以降は未定義。fixtureは1のみ対応 | 1 |

tag 3は繰り返すu8で、wire順に使う。応答は選択値の長さ(u16)、選択値、繰返し数(u8)、繰返し値。
選択値にはcount付き値のcount自身も含む。この形は検査用拡張だけの契約で、SPECへ操作番号を追加しない。
繰返し数の上限は255で、超過はそのtagのunsupported。実際のframe上限も別に掛かる。

同じtagの文脈別解釈と状態を、異なるfn/opへ交互に要求して確認する。
既知tagはcriticalに関係なく同じ形・値を使う。未知optionalは無視、未知criticalは受け取ったtagをechoしてunsupported。
既知の幅/数/禁止値違反はmalformed、予約値・fixture未対応値はunsupported。
拒否前後の状態は同じである。重複の最初の値と繰返し順序も確認する。

```python
import json
from pathlib import Path
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_tlv_requests import TlvRequestChecks
from oep_client.virtual_tlv_request_model import TlvRequestModel

registry, identity = spec_identity("/explicit/path/to/oep-spec")
model = TlvRequestModel(lambda: 0, boot_id=17)
checks = Checks(model.handle, registry, "virtual-resource-1")
report = TlvRequestChecks(checks, model.request_state).run()
report["spec"] = identity
Path("/explicit/output/tlv-requests.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

uv環境で実行する。単体はuv run pytest -q tests/test_conformance_tlv_requests.py。
public checkerはprivate台帳や隣接SPECを探索しない。raw交換と状態の判定を保存する。
独自fixtureではそのschema・state観測・source/版も保存する。
PASSはこの4文脈に限る。全要求schema、並行性、全core/共通/OEP固有規則、物理Probeは証明しない。

Hostが重要な項目へcriticalを付けることは別の責任である。
[Host要求のcritical指定](host-request-conformance.ja.md)では、実際に組み立てたpayloadを検査する。
このProbe側のPASSからHostの送出方針の適合を推論しない。
