# core clockの検査とlogical fixture

`ClockChecks`はcore §2.6a/4.1/5.2/6/7.7の第一段階の補助検査である。
選択したSPECを明示し、公開Pythonパッケージの`ClockModel`と`SampleClockAdapter`で実行する。
privateベンチ、設備設定、隣接リポジトリの自動探索は必要ない。

9項目（前提1、動作8）で次を確認する。

- 起動相対のu64 nsを、0、u32境界、浮動小数点の精度境界、符号境界、u64上端付近で保持する。
- 同じSID 0/corrのclockを繰り返しても、現在の時刻を返す。
- 受信と送信の間で実際に時計を読んだ値を返す。範囲内の推定値も不合格にする。
- SID 0のclockは所有者、期限、lease、corr履歴、再送cacheと資源を変えない。
- 所有セッションのclock再送は、保存済み応答をそのまま返し、新しい採時結果で応答を作り直さず、期限更新もしない。
  再送では古い時刻が正しいため、新規要求の採時検査を適用しない。内部処理で時計を読むこと自体は禁止しない。
- 観測clockではleaseが延びず、期限で所有権が切れる。
- boot_id 0と最大u32も普通の値としてconfirm/open/clockで一致する。
- malformed/critical TLV、セッション無しの非零SIDと他所有者のSIDを拒否する。

fixtureは制御できる正確なns時計、受信/送信の論理時刻、採時記録、所有状態を提供する。
実行と配送に7/13 nsの遅延を明示するが、採時の特定の位置を要求せず、区間と実際のread記録を照合する。
モデルの分解能を1 nsに選ぶことは、実機に1 ns分解能を要求することではない。
新しいbootでは時刻の原点を作り直す。実機リセットとしてこの制御を使わない。
資源の非変更検査には`resource-conformance.ja.md`のsample拡張を使う。

client checkoutのルートで実行する。

```sh
uv run pytest -q tests/test_conformance_clock.py
```

正常モデル、12種類の不具合注入、無効な前提3種類、許される内部採時2種類を合わせて18件で検査器も確認する。
raw交換を保存する例（SPECのcheckoutと新規出力先を明示する）:

```sh
uv run python - /path/to/oep-spec /path/to/clock-report.json <<'PY'
import hashlib, json, sys
from pathlib import Path
from oep_client import virtual_clock_model
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance_clock import ClockChecks
from oep_client.conformance_clock_sample import SampleClockAdapter
from oep_client.virtual_clock_model import ClockModel
registry, identity = spec_identity(sys.argv[1])
model = ClockModel()
checks = Checks(model.handle, registry, 'virtual-resource-1')
report = ClockChecks(checks, SampleClockAdapter(model)).run()
report.update(spec=identity, wire_trace=checks.trace,
              checker_sources_sha256=checker_sources_sha256(),
              model_sources_sha256={name:hashlib.sha256((Path(virtual_clock_model.__file__).parent/name).read_bytes()).hexdigest()
                  for name in ('virtual_clock_model.py','virtual_resource_model.py','virtual_core.py','virtual_stream_model.py','endpoint.py','virtual_bench.py')})
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed')
PY
```

実clockの精度/分解能、transport writerの実際の境界、全拡張のtimestampとの一致、
遅延/並行writer、core/共通の全規範とproduction Host統合は未検査である。
この9項目のPASSだけでcore準拠全体を主張しない。
