# データ通知とまとめ送りの検査

SPEC core §11.2/11.3の、data/event共通seq、データの形、min_bytes/max_delayを検査する。先に検査側の刺激と期待値を定め、独立したin-processサンプルへ適用する。実機transport、応答優先、送信queue上限、dropによるseq欠落は別段階。

条件は明示した2 fn、既知bytesのデータ刺激と既知markerの出来事刺激、全raw通知の受信。データ位置はinterfaceが決めるので、adapterは外部刺激の開始位置を返す。probeが返したpositionを期待値へ転記してはいけない。共通検査はdata見出し、position:u64、len:u16、後続TLVと、データ内容を独立に検証する。

検査する契約は、両方0で即送信、min_bytesのみ、delayのみ、どちらかの条件で送信、最初のbyteからのdelay、eventをmin_bytesに数えない、data/event共通seq、fn別seq、max_frame以内の分割、まとめ送り後の次のbatch、subscribe再送が未送信データ/seqを壊さない、session終了時に旧データを引き継がないこと。データの分割数は固定せず、位置と連結bytes、各フレームのseqと長さを検査する。

## サンプル契約

`io.github.open-embedded-probe.stream` revision 1、fn 1/2、instance 0/1。購読サンプルの資源op 16〜19、必須subscribe/unsubscribe、kind 1 / marker:u32に加え、byte列のdata通知を持つ。標準OEP interfaceではなく、規範registryには追加しない。

外部の合成ストリームはfnごとにbyte位置を持ち、刺激するbyte数だけ進む。購読とseqリセットでストリームの位置は戻さない。モデルへは期待位置とbytesを外部入力する。1回のsubscription内ではデータを到着順に連結し、最初のbyteからdelayを測る。閾値を満たした時点でmax_frameに収まるdata通知へ分割する。subscribe置換/unsubscribe/session終了は、このサンプルでは旧購読の未送信データを破棄する。これはサンプルのbuffer方針であり、全interfaceへの新しい規範を加えない。

サンプルのop/資源/エラー/未知値/TLVの扱いは購読・資源サンプルを参照する。固有status/reject/describeタグ/pin role、外部仕様、チップ固有手順は持たない。モデル内の蓄積bufferはtransport送信bufferではなく、その容量制限や非同期応答順序を検証したことにはならない。

## adapterと実行

`DataChecks`は既存の購読18項目に、data adapterの前提検証と14データ契約を加える。2 fnのサンプルは計33項目。従来のコア49項目・資源16項目も同じモデルへ別途適用する。通常CLIへ自動追加せず、補助レポートは常に`full_conformance=false`。

adapterは[購読adapter](subscription-conformance.ja.md)のfunctions / stimulate / receive / decode_eventに、次を提供する。

- `feed(fn, data)`：指定したbytesを外部から供給し、外部で分かる先頭のpositionを返す。probeの応答や内部cursorを期待値の根拠にしない。
- `loss_free=True`：今回の刺激量と観測条件でdropが起きない設備を、明示的に宣言する。データ分割はmax_frame×2＋17 byteの刺激を含むため、実機adapterは刺激中もtransportを継続受信するなど、送信queueを圧迫しない手段が必要。
- `observation_ms`：このデータ検査では1〜50ms。既知の400ms delayを、到着前後で分けて観測するための設備条件。

SPECではqueueに入らない通知のdropが認められる。この検査はその挙動を禁止する新しい規範ではなく、dropが無い刺激条件で内容・seq・まとめ送りを確認するもの。条件を満たせない設備にそのまま適用しない。宣言した条件でdropやbytes欠落があればその実行をFAILとし、規範で認められたdrop自体の検査とは区別する。

実時計ではOSのmonotonic時計を使う。仮想時計では`wait`と`clock_ms`を明示的に同じ外部テスト時計へ接続する。最初のbyteからのdelay検査は、受信時にfirstのdeadlineを過ぎ、lastのdeadlineには達していないことも記録・検証する。遅い観測で両方を過ぎてしまった実行をPASSにしない。OR条件のmin_bytes側もdelay以前の観測を確認する。無通知のPASSは指定した観測窓に対する結果。

`virtual_data_model.DataModel`は独立したprobe側モデル。`conformance_data_sample.SampleDataAdapter`は検査側のbyte cursorを持ち、外部入力した位置から期待値を作る。raw通知は検証前に保存する。壊れたframe、欠落/重複bytes、未知fn、IO失敗もskipへ変えない。IO失敗後は後続をBLOCKEDにする。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance_data import DataChecks
from oep_client.conformance_data_sample import SampleDataAdapter
from oep_client.virtual_data_model import DataModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    model = DataModel()
    checks = Checks(model.handle, registry, 'virtual-stream-1')
    adapter = SampleDataAdapter(model, functions=(1, 2), observation_ms=50, loss_free=True)
    report = DataChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

新規JSONを`open('x')`で予約し、SPEC/検査器/モデルsource hash、日時、raw要求/応答/通知、データ刺激と期待位置、観測時間を保存する。既存`.env.example`のSPEC/共有lock項目を使う。モデルはADDRESS/FRAMING/TCP_PEERを使わず、私的ベンチや実機を探索しない。

単体検査はu64 position/u16 lenのliteral bytes、壊れた通知、閾値/時刻/seq/再送/旧bufferのmutant、再起動を含む。seq一周では65,538個のdata/eventを交互に生成・復号し、65535→0を検証する。内部counterをseedしていないが、これは仮想時計・in-process配信の検査であり、実時計の33項目や実機wireのseq一周の証拠とは区別する。

応答優先、通知経路/切断、送信queue上限、dropのseq消費、物理非同期動作、未指定fn、実時計実行での再起動とseq一周は残件。
