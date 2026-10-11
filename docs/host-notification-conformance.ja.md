# Hostの通知振り分けと期限の検査

core §2.4/11.1/11.2のHost側を、明示したreaderアダプタと論理時計で検査する。
production linkの置換は行わず、実装追従前の検査・仮想サンプルである。
private設備設定や隣接SPECは探索しない。

採用例はfn 7/8、既知event kind 1（固定部分1 byteとTLV）、標準data形。
role 0x02だけをcorrと照合する。0x03/0x04の同じ位置はfnである。
未知role・要求role・待っていないcorrの応答を捨てる。
未知kindの固定部分は解釈せず捨てるが、data/event共通のfn別seqには反映する。
65534からの明示した読出し開始位置で65535→0→1を確認する。
seq 0→2→3で本当の欠落1を検出し、fn 8と混同しない。

adapterのAPIは`run(receive, clock, deadline=..., corr=..., expected=...)`。
receiveは絶対期限を受け取りraw frameまたはNoneを返す。clockは論理ms。
corr=Noneは受信pump、整数はそのcorrの応答待ちを採用する。
expectedは各fnの次のseqで、この値とclockはテスト側から明示する。
返すmappingはreply（bytes/None）、notifications（既知frameのlist）、expectedとgaps（fn別整数）。
このAPIは検査用アダプタの形で、Host一般の公開APIを規定しない。

時計は1 receiveにつき最大5 ms進み、指定した読出し期限で切り詰める。全体の絶対期限は20 ms。
一回の読出しを短い期限で区切る実装も許容する。
既知通知と未知eventが連続する場合でも、応答待ちとpumpが期限を延ばさないことを確認する。
20 readの刺激上限で終了しないアダプタも失敗とする。この上限はOEPの容量規定ではない。
サンプルのgap計算は順方向の試料を対象とし、重複・逆順・再接続の処理は確認しない。

```python
import json
from pathlib import Path
from oep_client.conformance_host_notifications import HostNotificationChecks
from oep_client.conformance_host_notifications_sample import SampleHostNotificationReader

report = HostNotificationChecks(
    SampleHostNotificationReader(), spec="/explicit/path/to/oep-spec"
).run()
Path("/explicit/output/host-notifications.json").write_text(json.dumps(report, indent=2))
assert report["status"] == "passed"
```

uv環境で`uv run python example.py`として実行する。単体は
`uv run pytest -q tests/test_conformance_host_notifications.py`。
単体ではSPEC identityを注入し、実checkoutを指定した利用時はcommit・dirty・registry SHAを記録する。
reportにはraw刺激・読出し期限・終了時刻・結果・checker/sample SHAを保存する。
独自アダプタのsource/版は利用者が追加して保存する。

5項目のPASSは、production Host、実時計/OS scheduling、全event形、破損frame、
回復/再送、Probe通知送出の適合を意味しない。full_conformanceは常にfalse。
未知kindを送り込む試料はHostの将来値への耐性を調べるもので、Probeが未割当kindを送ってよいという仕様ではない。
