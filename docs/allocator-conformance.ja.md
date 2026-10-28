# 資源番号の上限と再起動の検査

`AllocatorChecks`はcore §2.3/5.2/6/9に基づく第一段階の補助検査である。
公開`AllocatorModel`と`SampleAllocatorAdapter`を使い、SPEC checkoutと出力先を明示する。
privateベンチや隣接checkoutは探索しない。資源opの符号化はresource-conformance.ja.mdの独自sampleを採用し、
資源番号だけはcore §9のとおりu32で運ぶ（create応答・close/use要求・snapshotの番号がu32、上限0xFFFFFFFF）。


- 0xFFFFFFFE/0xFFFFFFFFを異なるfn/kindへ割り当て、全slotでそれ以上のcreateをunavailable・cause 2とする。
  既存資源はuse/closeでき、種類違いはcause 6、閉じた番号はno_resource。
- closeして空きができても、使い切った番号は同じbootで再使用しない。
- endと1000 msのlease境界で資源を解放し、旧createの再送は歴史的な番号を返しても資源を復活させない。
  新しいsessionでもallocatorは使い切ったまま。
- forceで新しいsessionへ移っても番号を戻さず、旧資源は引き継がない。
- 失敗したcreateの再送は保存した同一応答で、allocator/資源を変えない。
- 再起動でboot_idを変え、資源と履歴を無効化して番号を1から始める。

境界seed 0xFFFFFFFE/0xFFFFFFFFと時計はlogical fixtureの明示制御であり、実機opではない。
seedを設定するのはfresh/空のallocatorだけ。この検査で2^32−1回の実割当を行ったとは主張しない。
通常の容量不足と区別し、sample容量8に対して同時資源は最大2で試す。
予約だけで失敗した番号を欠番にしてよい規則は変更せず、一般の作成失敗でnext_id不変は要求しない。

client checkoutのルートで実行する。

```sh
uv run pytest -q tests/test_conformance_allocator.py tests/test_conformance_resources.py
```

新しいpytestは正常モデル、10種類の不具合注入、前提拒否3種類、重複causeの最初の値を読む2種類、u32境界1件の17件。
既存資源検査と合わせて36件。選択SPECのraw交換を保存する例:

```sh
uv run python - /path/to/oep-spec /path/to/allocator-report.json <<'PY'
import hashlib, json, sys
from pathlib import Path
from oep_client import virtual_allocator_model
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance_allocator import AllocatorChecks
from oep_client.conformance_allocator_sample import SampleAllocatorAdapter
from oep_client.virtual_allocator_model import AllocatorModel
registry, identity = spec_identity(sys.argv[1])
model = AllocatorModel()
checks = Checks(model.handle, registry, 'virtual-resource-1')
report = AllocatorChecks(checks, SampleAllocatorAdapter(checks, model)).run()
report.update(spec=identity, wire_trace=checks.trace,
              checker_sources_sha256=checker_sources_sha256(),
              model_sources_sha256={name:hashlib.sha256((Path(virtual_allocator_model.__file__).parent/name).read_bytes()).hexdigest()
                  for name in ('virtual_allocator_model.py','virtual_resource_model.py','virtual_core.py','endpoint.py','virtual_bench.py')})
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed')
PY
```

seed無しの全2^32−1割当、失敗予約、他のgeometry、並行allocatorと実資源、全core/共通規範は未検査である。
この検査だけで資源寿命全体・実機適合を主張しない。
