# 回復中のforceと経路単位の結果不明化

core §5.2は、再送を諦めた経路の未解決要求をすべて結果不明とする。受信済みの確定結果を捨てたり、別経路の要求まで諦めたりはしない。回復時のboot/session確認が終わるまでは、旧資源の一覧を次の操作へ使わない。

`RecoveryRaceChecks`は[recoveryの22項目](recovery-conformance.ja.md)＋制御前提1項目＋force 3項目＋batch 1項目の27項目。batch条件`none`、`prefix`、`queued`は、明示した新しいmodelでそれぞれ実行する。一つの結果で全条件を検査したとは扱わない。通常core検査も別に実施する。

## 読み出し中のsession差し替え

`SampleRecoveryRaceAdapter`が明示的にarmした地点で、fixtureだけがpeerから自分のSのboot/holderを確認して別IDのTへforceする。Tには別fnの資源を作る。Sはprimaryから送るままで、peerへ移さない。

- 最初のS clock直前: lockedを受け、一覧を採用しない。
- 最初のfnの一覧応答を受けた後: Sの一覧とTの一覧が混在しても、最後のS clockでlockedを検出して全体を捨てる。
- 最後のS clock直前: 一覧取得後の差し替えでも、一覧を捨てる。

wire-only回復hostはsession-unavailable/unknownを返し、候補資源を返さない。Tの資源・allocator・cache/high/last・lease・購読は、fixtureのT作成直後のsnapshotと完全一致する。回復hostのraw要求は確認と読み出しだけで、force/create/endはfixtureのpeer交換として分けて記録する。単独writerの通常条件へ任意の並行writerの保証を加える検査ではない。

## 複数未解決要求の放棄

primaryに成功するcreate、不正kindのcreate、成功するcreateの3件を受付枠内で送る。別経路のpeerにはprimaryと同じ数値corrのSID 0 clockを未解決で配置する。corrの重複禁止は同じ経路の未解決要求についてであり、別sessionでも同経路なら重複禁止。

| 条件 | fixture側の処理 | hostが受け取る応答 | primaryを諦めた後 |
| --- | --- | --- | --- |
| `none` | 3件を処理し、全応答を記録 | なし | 3件すべてunknown |
| `prefix` | 3件を処理し、全応答を記録 | 先頭1件だけ | 先頭のcompletedを保持、残り2件はunknown |
| `queued` | 3件を受理するが実行しない | なし | 3件すべてunknown |

sampleの操作待ち・論理転送時間は0ms。最初の未解決要求の待ちとして独立時計で1050msを確保してから経路を諦める。後続要求が一件ずつtimeoutしたとは扱わない。経路を諦める規則により、残りの未解決要求もunknownにする。

実行済みの成功/拒否と、未実行の要求は、hostからは区別しない。fixtureのraw応答とallocatorで実際の処理を確認するが、その情報はhostへ渡さない。sourceのprimaryをcloseし、未実行キュー・送信待ち・受付creditを破棄する。peerの受付creditとhostのpendingは保持し、peerの応答を通常どおり受け取る。

peerのSID 0 inventoryで現在の状態を確認しても、unknownだった個々の操作のoutcomeを復元しない。Sをpeerへ移して変更操作を再送しない。最後のTへのforce/endは、自分のSを解放するための明示fixture cleanupで、回復hostの自動動作ではない。

## sample hostの記録

`SamplePendingHost`はモデル/transportへアクセスせず、要求bytes、経路、boot、session、corrと受信済みの応答を記録する。

- per-routeのmax_frame/window/max_inflightを守り、同経路の未解決corrの重複を拒否する。
- abandonはその経路のpendingをすべてunknownにする。受信済みのcompleted/rejectedと、別経路のpendingを保持する。繰り返しても結果を変えない。
- result_lost応答を実際に受信しても、元の操作をrejectedと断定しない。statusはunknown、観測したresult_lostのraw応答は保持する。
- ローカルなreader世代を進め、旧readerの遅延応答を採用しない。これはwireに新しい番号を加える仕様ではない。
- 放棄後は新しい要求を受け付けない。明示resumeにはtransport回復済みと確認済みbootが必要。旧readerの応答は、新世代でcorrが同じでも採用しない。放棄した未解決sessionのIDを再使用しない。

このledgerは、decoderで取り出したmessageの記録処理であり、physical framingや操作固有payloadの検査を実装しない。検査器はbatchの応答header・createの資源ID/順序・peer clockのboot/payloadを別途検査する。ローカル世代の切替だけでTCP再接続やUSB再同期が完了するわけではない。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_recovery_races import RecoveryRaceChecks
from oep_client.conformance_recovery_race_sample import SampleRecoveryRaceAdapter
from oep_client.virtual_retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    for mode in ('none', 'prefix', 'queued'):
        adapter = SampleRecoveryRaceAdapter(RetentionModel(), batch_mode=mode)
        checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
        report = RecoveryRaceChecks(checks, adapter).run()
        assert report['status'] == 'passed', report
```

`.env.example`の既存SPEC/共有lockを明示する。公開側に私的ベンチ探索や設備項目を加えない。全raw交換・force地点・host ledger・状態比較・独立待機窓を保存する。

`full_conformance=false`。physical timeout/framing/reconnectとreader停止、production Host統合、任意の並行writer/atomic snapshot、他のbatchサイズや保持上限、任意の拡張の回復意味論は未検査。SPECとArduino firmwareは変更していない。
