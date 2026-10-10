# corr枯渇時の複数pending放棄と新しい経路での回復

`ExhaustedChecks`は制御前提1項目＋保持検査11項目＋batch回復1項目の13項目。none/prefix/queuedの3条件をそれぞれ新しい明示modelで検査する。core §4.1/4.4/5.2/6に基づくsample hostの補助検査で、production Hostや物理再接続の実装ではない。

corr 65533のcreate、65534の不正kind create、65535のendを受付枠内で送る。noneは処理済みの全応答を失い、prefixは先頭の成功だけ受信し、queuedは受理済み未実行のまま保持する。最初の未解決要求について1050ms待った後、経路全体を放棄する。後続要求それぞれのtimeout成立を主張せず、経路放棄によってpendingをunknownにする。

- 受信済みの成功とそのraw応答を保持する。未解決要求はすべてunknownにし、繰り返し放棄や旧readerの遅延応答で結果を変更しない。
- 旧session IDと経路を退役させ、host側の旧資源bindingを無効にする。旧経路を閉じ、未実行queueと送信待ちcredit/outputを破棄する。
- `SampleExhaustedHost`は別の論理経路で、明示barrier、SID 0の個体/boot、lock_state、sample inventoryを確認する。旧Sを新経路へ送らない。読取り中にbootが変われば停止する。
- ロックが残っていればlock-heldで停止する。所有者を推測せず、forceや旧endの再実行をしない。queuedのfixtureだけが既知の3000ms leaseの期限切れを追加待機で確認してから、回復を再試行する。
- 同じbootで解放を確認できた場合にだけ、未使用の暗号学的乱数IDを選び、新しい経路でopen 1を送る。lease/boot/TLVと応答の対応を確認してから利用を再開する。
- 新しいopenの応答を失ったら、新経路も隔離してunknownにする。自動再送、force、別IDでの再openをしない。

解放を観測しても、旧endが実行されたか期限切れで解放されたかを推測しない。元の要求のunknownを成功/拒否へ書き換えず、旧資源を自動再作成しない。同bootの資源allocatorは引き継ぎ、新sessionのcorr/履歴は1から始める。

fixtureは接続closeと別経路を論理APIで表現する。physical reader停止、USB再同期、TCP再接続を実装した保証ではない。経路上の読み出しはatomic snapshotではなく、並行writerがいない条件に限定する。新openにはforceを使わないため、他の所有者が先に取得した場合に奪い返さない。

単体22件ではbarrier無視、旧IDの再使用、unknownの書換え、受信済み結果の消去、旧binding保持、force使用の6欠陥を検出する。入力/制御能力不足、別の所有者のロック、boot変更、読取り中のboot変更、新open応答喪失も確認する。検査器はhostの要求ログとadapterの実際の送信bytesを照合する。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_exhausted import ExhaustedChecks
from oep_client.conformance_recovery_race_sample import SampleRecoveryRaceAdapter
from oep_client.virtual_retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    for mode in ('none', 'prefix', 'queued'):
        adapter = SampleRecoveryRaceAdapter(RetentionModel(), batch_mode=mode)
        checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
        report = ExhaustedChecks(checks, adapter).run()
        assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示する。私的ベンチへの依存・探索や新しい設備項目はない。[同じ接続でのend/open喪失回復](session-loss-conformance.ja.md)とは異なり、この検査は旧経路を退役させるため旧要求を再送しない。

`full_conformance=false`。production Host統合、物理reader停止/再接続、並行writer、任意の拡張の外部副作用、別のbatchサイズ/保持条件は未検査。SPEC・仮想probe・Arduino firmwareは変更しない。
