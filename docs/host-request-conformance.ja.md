# Host要求のcritical指定を検査する

core §2.3は、無視されると要求の意味を失う、または害のあるTLVにHostがcriticalを付けることを要求する。
Probeが未知criticalを拒否する検査だけでは、Hostの付け忘れを証明できない。
HostRequestChecksは、明示したencoderが実際に返すpayloadを独立した期待byte列と比較する。
production Hostを変更せず、検査と明示encoder例を提供する。

encoder契約は`encode(interface, operation, fixed: bytes, fields: list[tuple[int, bytes]]) -> bytes`。
fieldsのtagはcriticalを含まない番号1〜127。固定部分は呼び出し側が組み立てる。
encoderは文脈の方針に従ってbit7を付け、長さu16 little-endian、値と並び順を保存する。
番号0・128以上、65536 byte以上の値はValueErrorで断る。
このAPIの入力制限は検査部品の採用契約であり、既存Host APIを変更する要求ではない。

## レビューした12文脈

| 文脈 | criticalを付けるTLV | criticalを付けないTLV | 根拠 |
|---|---|---|---|
| core open | — | owner 1 | core §6.4 |
| rvswd scan | max_speed 1、idle_clock 4 | — | debug §1、§3 |
| rvswd attach | max_speed 1、pins 3、idle_clock 4、reset 5 | — | debug §1、§3 |
| swio scan | max_speed 1 | — | debug §1、§3 |
| swio attach | max_speed 1、pins 3、reset 5 | — | debug §1、§3 |
| swd scan | max_speed 1、targetsel 6 | — | debug §1、§5、core §2.3 |
| swd attach | max_speed 1、targetsel 2、pins 3、reset 5 | — | debug §1、§5 |
| logic configure / query（2文脈） | multirate 0x60 | mode/rate/samples/segments/trigger/pretrigger | capture §3.3、§5.2 |
| analog configure / query（2文脈） | — | 上記基本設定、frontend | capture §3.3 |
| capture-group bind | — | trigger_track 1 | capture §4 |

swd scanのtargetselは、coreの「無視されると要求の意味が無い」条件に当たる。
scanの指定したmultidrop targetを試す意味を保つためcriticalとする。
attachのtargetselとscanのtargetselは番号が異なる。文脈間で番号だけの方針cacheを共有しない。
captureの基本設定はすべて実装する規則があるため、SPECが明示的にcriticalなしとする。
multirateは実装しないProbeがあり、基本layoutでの取得への誤った代替を防ぐためcriticalとする。
ownerは表示用の任意情報なので非critical。すべてにbitを付ける方針ではこの仕様を満たさない。

core/debug/capture本文SHAとregistry SHAをレビュー時の値へ固定する。
番号・文脈名・revisionの整合も確認し、変われば再レビューまで停止する。
公開検査はprivateベンチや隣接SPECを自動探索しない。SPECと出力先を明示する。

```python
import json
from pathlib import Path
from oep_client.conformance_host_requests import HostRequestChecks
from oep_client.conformance_host_request_sample import encode

report = HostRequestChecks(encode, spec="/explicit/path/to/oep-spec").run()
Path("/explicit/output/host-request-policy.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

5項目はcritical、noncritical、順序/文脈、値のコピー、無効入力。
毎回の入力・期待payload・実際のpayloadを保存する。無効入力はtag/長さと拒否を保存する。
繰り返すfrontendとmultirateを異なる値で送り、同じtag1をwire/core/groupへ交互に送る。
個別項目の省略はtail組み立てだけの検査で、必須TLVを省略した完全な要求の成功を主張しない。
0/256/65535 byteの値は値を加工しない部品検査であり、各TLVの意味・幅、transport上限の適合とは別。

単体は`uv run pytest -q tests/test_conformance_host_requests.py`。
68件は正常1、各文脈の各tagのbit反転46、意味的故障13、本文変更3、registry/schema変更5。
意味的故障は一律critical、一律非critical、並べ替え、重複除去、固定部分消失、値書換え、
長さのendian/幅、省略、入力制限の破り、tagだけのcacheを含む。

このPASSは12文脈のtail組み立てを証明する部分検査（full_conformance=false）。
別拡張はその拡張の意味から重要項目をレビューし、同様のoracleを作る。
plan、config、gpio drive等の重要度台帳、全要求schema、要求する機能の選択・必須引数/default、
Probeの拒否/非変化、全固定部分の組み立て、production encoder、実機の電気的安全は未証明。
外部encoderを使う場合は、そのsource/版・adapterと実行環境も別途保存する。
Probe側の未知/既知TLV規則は[要求TLV検査](tlv-request-conformance.ja.md)、
応答読出しは[core応答形検査](host-core-shapes-conformance.ja.md)で扱う。
