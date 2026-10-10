# corr枯渇前のsession切替の補助検査

core §4.1/5.2/6は、同sessionのcorrを一周させず、end用の番号を残して異なるsession_idでopenすることを定める。endで旧資源を解放するが、元の結果が不明な操作のoutcomeを復元するものではない。

`RolloverChecks`は[corrの17項目](corr-conformance.ja.md)＋制御前提1項目＋切替5項目の23項目。`SampleRolloverHost`は明示retention sampleでwire交換を行うhostの例で、production Hostへ自動切替を組み込む変更ではない。保持16 byte、履歴8件、資源操作はsample条件。

## 切替手順

- 通常の新要求は65534までとし、65535はendのために残す。自動採番は受付が成功した後に進め、受付拒否で番号を消費しない。
- 同経路にpendingがあれば切替を拒否し、先に応答を処理する。別sessionのSID 0要求も含め、その経路のpendingを確認する。放棄済みの経路でも切替を拒否し、transport回復を切替で代用しない。
- 新IDは暗号学的な乱数で、0や過去に使用したIDを避けて選ぶ。番号を使い切った後や不正な新IDでは、endを送る前に停止する。
- endを送り、completed successとTLVを確認する。65534まで使用した場合はendが65535となる。旧資源のhost側bindingを無効にする。
- 旧readerをローカル世代で退役させる。旧要求の確定結果とunknownの記録は書き換えない。boot/個体を確認し、明示したreader barrierで残り応答/creditがないことを確認する。
- bootが変わった場合やbarrierが失敗した場合は、新しいopenを送らない。
- 同じbootとbarrier完了を確認できた場合だけ、新IDのopenをcorr 1で送り、lease/boot/TLVを確認する。以後はcorr 2から進める。旧資源を自動作成し直さない。

reader世代はhostのローカルな記録で、wireへ追加する番号ではない。今回のbarrierは同じ論理経路で全end応答を受け、fixtureの未受信messageとcreditが空であることを確かめる。physical reader停止やTCP再接続、USB再同期を実装したという意味ではない。

## 検査ケース

| ケース | 確認すること |
| --- | --- |
| 確定した旧create | 最後のcreateは65534、endは65535、新openは別IDの1。旧binding/資源/履歴を引き継がず、同bootの新資源IDは続きから発行する。旧IDのuseはno_resource。 |
| 結果を失った旧create | 要求17 byteの応答をhostへ渡さず1050ms待つ。同一再送はresult_lost。transport上の応答は受信済みでも元のoutcomeはunknownのまま、切替後も記録を保持する。 |
| 未解決要求 | corr 65533/65534の2件を送信待ちにし、切替拒否がwire・counter・資源・cacheを変えないことを確認する。2応答を処理してから切り替える。 |
| end後のboot変更 | 明示logical restartをend応答後に挟み、新openを送らずboot-changedで停止する。旧bindingを返さない。 |
| reader barrier不成立 | endを確認しても新openを送らずreader-unavailableで停止する。旧bindingを返さない。 |

結果を失ったcreateの再送待ちは、sampleの操作待ち0ms/論理転送0msに対し1050ms。実機の操作・転送時間を含むtimeout検査は別途必要になる。

単体では、end用番号を使い切る、pendingを無視、旧bindingを保持、unknownを確定結果に書き換える、旧session IDを使う、boot変更を無視、barrierを無視する7欠陥を検出する。不正ID、枯渇後のwrap防止、放棄済み経路の切替拒否も確認する。

end/openの応答を論理的に失う負例では、automatic retry/forceや次のopenへ進まず、pendingをunknownにして経路を隔離する。これは停止を確認する負例で、timeout後の再同期/同一要求再送からの回復を実装する検査ではない。openを失った場合のTの解放は、単体fixtureの期限切れで行い、hostの自動処理と分ける。

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_rollover import RolloverChecks
from oep_client.conformance_recovery_sample import SampleRecoveryAdapter
from oep_client.virtual_retention_model import RetentionModel
from oep_client.hardware import equipment_lock

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleRecoveryAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = RolloverChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

既存`.env.example`のSPEC/共有lockを明示し、公開側から私的ベンチを探索しない。全raw交換、ledger、boot、資源/allocator/cache、高corr、reader barrierを新しい証跡へ保存する。

`full_conformance=false`。production Host統合、physical reader停止/再接続、end/open timeoutからの回復、番号枯渇時にpending経路を放棄した後の回復、任意の拡張の外部副作用、並行処理は未検査。SPECとArduino firmwareは変更していない。
