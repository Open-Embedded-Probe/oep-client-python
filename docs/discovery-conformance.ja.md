# core発見処理のcursor・channel名検査

`DiscoveryChecks`はcore §2.1/7.1–7.5に基づく第一段階の補助検査である。
通常の順次ページングに加え、listのページ途中のentryとfn 0 describeのページ途中のTLVを指すcursorを確認する。
privateベンチを参照せず、SPEC checkoutと出力先を明示する。

明示した`supported_revisions=(1,1)`プロファイルを渡すと、revision範囲検査を加える。

- listのfirstを0〜total、total+1（u16内）、65535で照合する。項目は最初のsnapshotの該当suffixで、末尾はcount 0。
- fn 0 describeのfirstも全TLV位置と末尾以降で照合し、moreとTLV境界を確認する。
- セッション保持中とend後にも同じcursor結果を要求する。
- channel_name（core §7.5）は先頭u16 channelを持ち、宣言したchannelsの範囲にあり、channelごとに1つある。名前は`A-Z a-z 0-9 _ -`の
  1〜16 byteで、probeの中で重ならない。ピンの呼び名（label）は設定の`channel.{}.label`（core §13.8）で、ここでは見ない。
- 存在しないfnのdescribeはfirstが末尾以降でもunknown_functionとなり、宣言を変えない。

最初のsnapshotだけが一貫して誤っている場合の全実装固有情報までは証明しない。
各インターフェースのname/instance/opsやdescribeはInterfaceChecksと組み合わせる。
検査するのは現在の経路のframeであり、他経路の最小frameを推測しない。

client checkoutのルートで実行する。

```sh
uv run pytest -q tests/test_conformance_discovery.py
```

pytestは正常3構成、cursor/locked/unknown-fnの不具合、channel名の不正を持つ。
正常構成は短い名前（複数entryが同じpageに入る）、48 byteの名前、fn無し。
公開のDeclarationModelへfixture条件を明示して使い、モデル本体の動作を変更しない。
これは検査用の独自インターフェースであり、標準OEP機能の適合例ではない。
SPEC identity、checker/model SHA、raw交換を保存する例:

```sh
uv run python - /path/to/oep-spec /path/to/discovery-report.json <<'PY'
import json, struct, sys
from pathlib import Path
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance.discovery import DiscoveryChecks
from oep_client.conformance.declaration_model import DeclarationModel
from oep_client.virtual_core import tlv
registry, identity = spec_identity(sys.argv[1])
reports = {}
for geometry in ('short', 'long', 'empty'):
    model = DeclarationModel()
    model.extension.name = b'io.test' if geometry == 'short' else b'io.' + b'a'*45
    if geometry == 'empty': model.extension.functions = ()
    d = registry['core']['tlv']['describe']
    model.ep.static[0] = tuple(r for r in model.ep.static[0] if r[0] not in (d['channels'], d['channel_name']))
    model.ep.static[0] += (tlv(d['channels'], struct.pack('<H', 2)),
                           tlv(d['channel_name'], struct.pack('<H', 0)+b'GPIO0'),
                           tlv(d['channel_name'], struct.pack('<H', 1)+b'GPIO1'))
    checks = Checks(model.handle, registry, 'virtual-resource-1')
    reports[geometry] = DiscoveryChecks(checks).run()
    reports[geometry]['wire_trace'] = checks.trace
report = dict(status='passed' if all(r['status']=='passed' for r in reports.values()) else 'failed',
              full_conformance=False, spec=identity, fixtures=reports,
              checker_sources_sha256=checker_sources_sha256())
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
