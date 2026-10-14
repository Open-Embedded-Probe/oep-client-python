# 循環corr作業版のコア大枠検査

`CoreSmokeChecks`は、現行作業版の選択した契約を、小さいraw messageの検査で確認するAPIである。
旧`Checks`のガードを解除したり、旧検査全部を新仕様の検査として走らせたりはしない。
選択したSPECのcore SHAとheader/corr定義を確認し、レビューしていない本文では送信前に止める。
レビュー済みの本文はoep-spec a09fa45の`docs/oep-core.ja.md`である。

```python
from oep_client.conformance import spec_identity
from oep_client.conformance_core_smoke import CoreSmokeChecks

registry, spec = spec_identity(spec_checkout)
checks = CoreSmokeChecks(send_message, registry, expected_unit, spec=spec,
                         observe_effects=observe_selected_effects)
report = checks.run()
```

`send_message`はOEP message bytesを送り、応答message bytesを返すcallbackである。
接続の排他、transportのframing、待ち時間、ログの保存、設備の選択は呼び出す側が用意する。
このAPI自体は機器の探索・接続・flashを行わず、他のプロジェクトの設定場所を参照しない。

5つのcaseでconfirm、core describe/list、clock、corr0の拒否、セッションの周回と終了を確認する。
confirmの応答はflagsなしの固定部分（revision、max_frame、window、max_inflight、boot_id）として読む。
fn 0のconfirm/list/describe/clock/lock_stateは任意の方針判定（unavailable cause 7）でも拒否されないため、成功以外を失敗とする。
open/keepalive/endを方針で拒否するprobeでは、呼び出す側が許可された経路と相手を用意してから実行する。
65534でopenし、65535→1で同じSIDのkeepaliveを続け、同corrの再送、endとその再送、終了済みSIDのopen拒否を確認する。
openには非critical ownerを付けるが、表示のechoは成功条件にしない。
保持した同一結果とresult_lostを両方許し、cache0を不適合にしない。
listはflagsなしの6byte entryを読む。名前付き機能なしでもよく、名前付き機能があれば選択したlistの形を確認する。

`observe_effects`は、対象の作用だけを独立に観測したJSON互換の状態を返すcallbackである。
再送前後で実行回数・leaseの更新回数などを比較する。無関係に増える時計やbackground counterを同じsnapshotへ混ぜない。
観測callbackが無い場合、応答比較だけでは二重実行防止を証明できないため、reportのuncheckedに残す。
テスト用端点のsnapshotを観測するunit testと、実機の独立観測は別の証拠である。

前提caseが失敗した後は追加要求を送らず、後続caseはnot_runとして記録する。
途中で終了できなかったSIDはunfinished_session、openを試したSIDはobserved.attempted_sessionへ残る。
転送の失敗ではopenが実行されたか不明な場合もあるため、呼び出す側が経路と状態を確認して回復する。
この検査はforceで他のセッションを奪わない。対象が使用中なら実行前に調整する。

全core準拠ではない。CRC/COBS/USB/TCP障害、leaseの全時間条件、fn 0の短い処理上限（core_quick_op_max_ms）、方針判定、force、owner表示を提供する場合の全規則、資源寿命、Hostの半周制限/旧応答排出、個別interface動作は別の検査である。
reportは常にfull_conformance=falseで、scopeとuncheckedを結果と一緒に扱う。
