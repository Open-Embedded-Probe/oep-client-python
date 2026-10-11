# ARM-ADIの独立検査とlogical fixture

`ArmAdiChecks`は選択したSPECのdebug §2/6とcore §2.3/4/5/7/9から、
raw-messageで`oep.target.arm-adi`を確認する第三段階の検査である。
一般インターフェイス検査（第二段階）も別に実行する。
現時点で31項目を持ち、ARM-ADI全体の適合を主張しない。

## 前提と構成

公開Pythonパッケージの`ArmAdiModel`と`SampleArmAdiAdapter`を使う。
privateベンチや設備設定、暗黙の隣接リポジトリ参照は使わない。
SPECのcheckoutは明示的に渡す。SPECのcommitとregistry SHA、checker/modelのSHA、
送受信rawとadapterの刺激・観測を結果に保存する。

このlogical sampleはfn 1/2/3をRVSWD/SWIO/SWD、4をconsole、5をARM-ADIに割り当てる。
これはsampleの選択であり、実機のfn番号の規定ではない。
接続・SID・replayは現coreの機構を使い、電気的なSWDを実行しない。
adapterのreset_scopeがlogical-modelでない、選択registryのop/ACK/statusが異なる場合は、
contextで断り、後続項目をblockedにする。実機へlogical resetを流用しない。

## 確認する条件

| 範囲 | 検査 |
|---|---|
| 宣言 | transfer/read_block/write_block、features無し、max_lengthが共通u16。sampleは16 byte |
| 空転送 | done 0/status ok/ack 0/nvals 0、targetへ触らない |
| 混合転送 | AP書込み・DP読出し、DP SELECTのhost設定を保持、読み値の個数と順序 |
| APの読み | 明示した以前のAP read結果、次のAP read、DP RDBUFFをrawの順序で返す。無関係なDP readでpendingを消さない |
| ACKと進捗 | WAIT/FAULT/無応答を各失敗位置へ注入。doneまでの読みだけ返し、失敗した転送・後続を実行しない |
| 再試行 | WAITからOKへの回復で、packetは繰り返すが書込みの実行は一度だけ。FAULT/無応答を自動再実行しない |
| 受付 | req予約bit、途中の切れた引数、count、critical/壊れたTLV、SID/所有権、資源型、最大応答を実行前に断る |
| 再送 | 同じcorrの要求は同じ応答を返し、target操作を増やさない |
| blockの基本 | 0語、16 byteの上限と超過、alignment、外部から変化したバス上のメモリを読む、write後のread、SELECT/CSW保持・TAR進行 |

WAITの3回という回数はこのfixtureの選択であり、OEPが全probeに固定する回数ではない。
最初のAP readの値に0を要求しない。fixtureは以前のread結果を非零の値で明示し、
probeがそれを削除したり勝手に並べ直したりしないことを確認する。
DP offset 0のread/writeを同じレジスタとは扱わない（DPIDR/ABORT）。

posted readの参照は[Arm IHI 0031G](https://documentation-service.arm.com/static/622222b2e6f58973271ebc21)、
B2.2.7（RDBUFF）とB4のSWD readである。OEPのtransferはこの遅延を隠さない。
RDBUFFを繰り返し読んだ場合のUNKNOWN値を特定の値として適合条件にしない。

## 実行

以下のコマンドはclient checkoutのルートで実行する。
pytestでは正常モデル、16種類の意図的な違反、無効な前提、max_lengthの誤った幅を確認する。

```sh
uv run pytest -q tests/test_conformance_arm_adi.py
```

SPECを選択したraw交換の保存例:

```sh
uv run python - /path/to/oep-spec /path/to/arm-report.json <<'PY'
import hashlib, json, sys
from pathlib import Path
from oep_client import virtual_arm_adi_model
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance_arm_adi import ArmAdiChecks
from oep_client.conformance_arm_adi_sample import SampleArmAdiAdapter
from oep_client.conformance_interfaces import InterfaceChecks
from oep_client.virtual_arm_adi_model import ArmAdiModel
registry, identity = spec_identity(sys.argv[1])
model = ArmAdiModel()
common = InterfaceChecks(Checks(model.handle, registry, 'virtual-resource-1')).run()
model = ArmAdiModel()
checks = Checks(model.handle, registry, 'virtual-resource-1')
report = ArmAdiChecks(checks, SampleArmAdiAdapter(model)).run()
report['arm_status'] = report['status']
report['status'] = 'passed' if report['arm_status'] == common['status'] == 'passed' else 'failed'
report.update(spec=identity, interface_checks=common, wire_trace=checks.trace,
              checker_sources_sha256=checker_sources_sha256(),
              model_sources_sha256={name:hashlib.sha256((Path(virtual_arm_adi_model.__file__).parent/name).read_bytes()).hexdigest()
                  for name in ('virtual_arm_adi_model.py','virtual_attach_model.py','virtual_dependency_model.py',
                               'virtual_resource_model.py','virtual_stream_model.py','virtual_core.py','endpoint.py','virtual_bench.py')})
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed' or common['status'] != 'passed')
PY
```

## 未検査とモデルの制限

blockモデルはバス上のメモリへのアクセスを抽象化し、MEM-APのpacket列を実装しない。
posted blockの並べ直し、TAR自動増加境界での書き直し、部分失敗・posted write FAULTはまだ検査しない。
AP bank/SELECTの完全なdecode、sticky状態とABORT、ADIv6、SWD attachのTARGETSEL/tid、
packetのparity/turnaround/idle、電気的な再試行と時間、CPUの実行状態は別途必要。
全経路のframe容量、u32番地を越えるblockの扱い、production Hostとの統合も未完成である。
core/共通の全規範の確認を、この第三段階のPASSで置き換えない。
