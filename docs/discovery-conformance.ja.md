# core発見処理のcursor・label検査

`DiscoveryChecks`はcore §2.1/7.1–7.5に基づく第一段階の補助検査である。
通常の順次ページングに加え、listのページ途中のentryとfn 0 describeのページ途中のTLVを指すcursorを確認する。
privateベンチを参照せず、SPEC checkoutと出力先を明示する。

明示した`supported_revisions=(1,1)`プロファイルを渡すと、revision範囲検査を加える。

- listのfirstを0〜total、total+1（u16内）、65535で照合する。項目は最初のsnapshotの該当suffixで、末尾はcount 0。
- fn 0 describeのfirstも全TLV位置と末尾以降で照合し、moreとTLV境界を確認する。
- セッション保持中とend後にも同じcursor結果を要求する。
- labelは先頭u16 channelを持ち、宣言したchannelsの範囲にあり、残りがUTF-8 textである。
  C0制御文字はprobe側の不合格条件にしない。表示時の置換はHostの別の契約。
- 存在しないfnのdescribeはfirstが末尾以降でもunknown_functionとなり、宣言を変えない。

最初のsnapshotだけが一貫して誤っている場合の全実装固有情報までは証明しない。
各インターフェースのname/instance/opsやdescribeは既存InterfaceChecksと組み合わせる。
検査するのは現在の経路のframeであり、他経路の最小frameを推測しない。

client checkoutのルートで実行する。

```sh
uv run pytest -q tests/test_conformance_discovery.py
```

pytestは正常3構成、cursor/locked/unknown-fnの不具合5種類、label不正5種類の13件。
正常構成は短い名前（複数entryが同じpageに入る）、48 byteの名前、fn無し。
公開のDeclarationModelへfixture条件を明示して使い、モデル本体の動作を変更しない。
これは検査用の独自インターフェースであり、標準OEP機能の適合例ではない。
SPEC identity、checker/model SHA、raw交換を保存する例:

```sh
uv run python - /path/to/oep-spec /path/to/discovery-report.json <<'PY'
import hashlib, json, struct, sys
from pathlib import Path
from oep_client import virtual_declaration_model
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance_discovery import DiscoveryChecks
from oep_client.virtual_declaration_model import DeclarationModel
from oep_client.virtual_core import tlv
registry, identity = spec_identity(sys.argv[1])
reports = {}
for geometry in ('short', 'long', 'empty'):
    model = DeclarationModel()
    model.extension.name = b'io.test' if geometry == 'short' else b'io.' + b'a'*45
    if geometry == 'empty': model.extension.functions = ()
    model.ep.static[0] += (tlv(0x46, struct.pack('<H', 0)+b'UART\nRX'),
                           tlv(0x46, struct.pack('<H', 3)+'端子'.encode()))
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    reports[geometry] = DiscoveryChecks(checks).run()
    reports[geometry]['wire_trace'] = checks.trace
report = dict(status='passed' if all(r['status']=='passed' for r in reports.values()) else 'failed',
              full_conformance=False, spec=identity, fixtures=reports,
              checker_sources_sha256=checker_sources_sha256(),
              model_sources_sha256={name:hashlib.sha256((Path(virtual_declaration_model.__file__).parent/name).read_bytes()).hexdigest()
                  for name in ('virtual_declaration_model.py','virtual_resource_model.py','virtual_core.py','endpoint.py','virtual_bench.py')})
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed')
PY
```

他経路のframe、firmware版を越えた不変性、並行性、Hostのtext表示と全core規範は未検査として残す。
この検査のPASSをcore全体の適合と扱わない。


追加のrevision範囲検査は、fixtureが扱うrevisionの区間を明示して使う。
revision1を含む整数tupleをsession操作前に確認する。現在の仮想宣言fixtureの期待区間は(1,1)。
境界0/1/2/254/255の25組で、包含範囲は最高対応revision、非対応範囲はunsupported＋tag0＋supported TLV、逆順はmalformedを要求する。
確認中も所有セッションを保持し、直近のclock再送は保存した応答又はresult_lost。keepaliveで所有権継続を確認する。
このプロファイルはprobe firmwareの最低版ではない。複数revisionのfixtureを実行した証拠として使わない。
経路ごとの両方向適用、他経路の宣言、すべての保持副作用は別の検査範囲である。

範囲を変える要求はProbeへ与える検査刺激である。通常Hostの再confirmは選択済みrevisionへmin=maxを固定する（core §7.1）。
検査刺激の後に既知revision1のconfirmへ戻してから、元のセッションのclock再送とkeepaliveを確認する。
