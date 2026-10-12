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

## レビューした16文脈

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
| plan_apply | role_assignment 0x10 | — | core §2.3、plan §2.1 |
| gpio set | drive 1 | — | core §2.3、fixture §1.1 |
| uart configure | format 1 | — | fixture §2、registry configure.format |
| config set | plan/label/idle/slot/uart/disable/wifi | — | core §2.3、config §1/2、今回のHost方針 |

swd scanのtargetselは、coreの「無視されると要求の意味が無い」条件に当たる。
scanの指定したmultidrop targetを試す意味を保つためcriticalとする。
attachのtargetselとscanのtargetselは番号が異なる。文脈間で番号だけの方針cacheを共有しない。
captureの基本設定はすべて実装する規則があるため、SPECが明示的にcriticalなしとする。
multirateは実装しないProbeがあり、基本layoutでの取得への誤った代替を防ぐためcriticalとする。
ownerは表示用の任意情報なので非critical。すべてにbitを付ける方針ではこの仕様を満たさない。

core/debug/capture/plan/fixture/config本文SHAとregistry SHAをレビュー時の値へ固定する。
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
85件は正常1、各文脈の各tagのbit反転56、意味的故障13、本文変更6、registry/schema変更5、新規4文脈のop guard4。
意味的故障は一律critical、一律非critical、並べ替え、重複除去、固定部分消失、値書換え、
長さのendian/幅、省略、入力制限の破り、tagだけのcacheを含む。

このPASSは16文脈のtail組み立てを証明する部分検査（full_conformance=false）。
別拡張はその拡張の意味から重要項目をレビューし、同様のoracleを作る。
全要求schema、要求する機能の選択・必須引数/default、
Probeの拒否/非変化、全固定部分の組み立て、production encoder、実機の電気的安全は未証明。
外部encoderを使う場合は、そのsource/版・adapterと実行環境も別途保存する。
Probe側の未知/既知TLV規則は[要求TLV検査](tlv-request-conformance.ja.md)、
応答読出しは[core応答形検査](host-core-shapes-conformance.ja.md)で扱う。


planの割り当てを無視した取得や、GPIOで指定のdriveを無視した駆動を避けるためcriticalを付ける。
UART formatも、指定した通信形を無視して8N1へ落ちないようcriticalにする。
config setの各項目については、このencoder例では「指定した項目を設定する」要求として全項目をcriticalにする。
保存するpresetも、使うHostが明示的に設定した値である。labelもnrst等の線探索に使うので、
この採用方針では表示だけのcore ownerと区別する。これらはcoreの意味から適用したHost方針であり、
config本文に各tagのcritical指定を新たに追加したわけではない。
同一set内のplan/wifiとdriveの複数キーを異なる値で送り、出現順とopaqueな値コピーを照合する。
wifiのtestpassは検査用の架空文字列。実ネットワーク・設備・保存設定は扱わない。
同時に成り立つ実機構成を検証せず、declared items、キー重複、無効channelや電気条件はProbe適合検査へ残す。

## 公開APIの送信箇所を確認する

`tests/test_conformance_host_critical_calls.py`は実HostとGPIO/UART/rvswd/swio/swdの公開APIを使う。
GPIO driveの2要素、UART format、wire attachのmax_speed/pins/reset、rvswd idle_clock、swd targetselを、
独立したliteral payloadと比較する。13種類の拒否tagをscripted peerからunsupportedで返し、
Hostが受信したcritical tagを保持して例外にし、項目を外して再送しないことを確認する。
各tagのcritical bitを落とす13故障も検出し、合計26件PASS。

公開interfaceクライアントには旧op番号が残っているため、この検査は現SPECのop番号の適合を判定しない。
初回に現SPECの16/17を要求した検査は、実際の1/2で失敗した。その差異は実装追従として残し、
coreのcritical方針の検査ではrole/corr/fn/sessionとpayloadを照合する。
現SPEC番号を使う独立encoderの16文脈検査と、この公開APIの部分検査を混同しない。
scan/capture/config/planを含む公開API全体、物理副作用、未知項目を無視する実Probeとの連携は引き続き未確認。
