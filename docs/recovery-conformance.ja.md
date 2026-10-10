# result_lost後の状態読み直しの補助検査

core §5.2は、実行済みか不明な変更要求を新しいcorrで自動再実行しないこと、再送を諦めた後はboot_idと状態を読み直すことを定める。result_lostは元の要求の成功・拒否・未実行のいずれも証明しない。読み直した状態も、元の結果そのものではない。

今回の`RecoveryChecks`は明示retention sampleの11項目＋制御前提1項目＋回復10ケースの22項目。probeの保持契約とsample hostの回復方針を分けて観測する。sample資源操作、状態の読み方、単独writer条件を一般の拡張へ義務付けるものではない。既存production Hostへ回復処理を組み込む変更ではない。

`SampleRecoveryAdapter.discard_reply()`は論理exchangeを完了させ、全raw応答を証跡に記録した上で、hostへは返さない。実機の転送・再起動を行わず、physical timeoutも起こさない。sample opは操作待ち時間を定めておらず0ms、論理転送時間も0ms。再送前には独立時計で1050ms待ち、coreのhost_wait_add_ms=1000を下回らないことを確認する。実機では操作待ちと転送時間を加算し、経路の再同期/再接続を別途検査する必要がある。

`SampleRecoveryHost`はモデルのstate/cache/allocatorへアクセスせず、公開wireのconfirm/identity、Sのclock、sample inventory(op 19、SID 0)だけを使う。同一要求の再送がresult_lostとなった後、次を確認する。

1. 個体とboot_idを確認する。bootが変われば旧資源との対応を破棄し、番号が同じでも結び付けない。
2. 同じ経路で新corrのSのclockを読み、現在のSの有効性を確認する。no_session/lockedなら旧sessionの資源との対応を返さない。lock_stateの「locked」だけで自分のsessionと断定しない。
3. sampleの各fnの資源一覧を読み、資源番号0、重複、型、長さを検証する。
4. boot/sessionをもう一度確認する。読み出し中の再起動・期限切れなら一覧を破棄する。複数fnの一覧はatomic snapshotではない。
5. 取得した状態を返す。元の操作のoutcomeは常にunknownのままにする。

回復中にcreate/close/open/end/forceや元の変更要求を送っていないことを、独立した検査器がraw要求一覧で検査する。モデル側の状態は検査器だけがoracleとして使い、読み取り結果との一致と資源/allocatorの不変を照合する。新corrのSのclockは通常の新要求なので、履歴とleaseを更新する。result_lost再送そのものは更新しない。

- 要求欠落、応答欠落、履歴追い出しで成功応答を失う: 同一再送はresult_lost。単独writerを明示したfixtureで同じslotへ資源が一つ増えた場合はobserved-addedとする。元のsuccess応答を復元したとは扱わない。
- 拒否応答を失う場合と、送っていない古い番号がresult_lostになる場合: unchanged-state。失敗・未実行とは断定しない。
- closeの成功応答を失う場合: observed-absent。別の型で拒否されたcloseの応答を失う場合: still-present。どちらも自動closeし直さない。
- 同じslotに二つ増えた場合や単独writerを保証しない場合: ambiguous-state。候補資源を選ばない。
- 期限切れ: session-unavailable。一覧や候補を返さない。
- 明示した論理再起動: boot-changed。一覧や候補を返さない。旧openを再送しない。

単独writer条件はこのfixtureの前提であり、一般的な資源増分の帰属を保証しない。close対象の現在の不在/存在も操作のoutcomeではない。外部副作用が状態へ現れない拡張は、拡張仕様に別の照合手順が必要になる。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_recovery import RecoveryChecks
from oep_client.conformance_recovery_sample import SampleRecoveryAdapter
from oep_client.virtual_retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleRecoveryAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = RecoveryChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを使い、私的ベンチの探索や新しい設備設定を追加しない。証跡には失った元のraw応答も保存するが、回復hostにその内容を渡さない。

`full_conformance=false`。物理損失/timeout/再接続、production Host統合、並行writerとatomicな状態取得、読み直し中のforce、任意の拡張の回復意味論、他の保持サイズは未検査。SPECとArduino firmwareは変更していない。
