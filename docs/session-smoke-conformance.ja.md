# lease・forceの大枠検査

`SessionSmokeChecks`は現行の循環corr作業版の選択したsession寿命をraw要求で確認する。
レビュー済みcoreのガードを持つ`CoreSmokeChecks`をcontextに使い、旧checkerのガードは変更しない。

```python
from oep_client.conformance import spec_identity
from oep_client.conformance_core_smoke import CoreSmokeChecks
from oep_client.conformance_session_smoke import SessionSmokeChecks

registry, spec = spec_identity(spec_checkout)
core = CoreSmokeChecks(send_message, registry, expected_unit, spec=spec,
                       observe_effects=observe_selected_effects)
report = SessionSmokeChecks(core, wait_ms=wait_or_advance_ms,
                           prepare_resource=create_selected_resource,
                           resources_released=selected_resources_are_released).run()
```

send/排他/framing/待ち時間/観測は[コア大枠検査](core-smoke-conformance.ja.md)と同じ。
このAPIは設備の探索、接続、flash、他の設定場所の読込みを行わない。
利用者が占有を許可した検査用の端点で実行する。FORCE caseは、この検査自身が作ったSIDから別のSIDへ所有を移す。
他のHostが取得したロックはこの検査でforceしない。最初のopenがlockedなら前提caseが失敗して後続を止める。

4caseで準備、再送と期限切れ、session判定後の拒否による期限更新、forceを確認する。
leaseは1000ms。700ms後に再送/SID0/別SIDの拒否を挟み、400ms後に期限切れを確認する。
期限切れ後の元のkeepaliveへの再送は過去の結果またはresult_lostを許し、ロックが復活しないことも確認する。
別caseでは700ms後に所有者の不正なkeepaliveを送り、さらに700ms後も所有が続くことを確認する。
force後には旧SIDの要求がlockedとなり、最後に新SIDのendを確認する。

`wait_ms(ms)`は、対象の時計を指定量進める、またはその時間待つ呼出し側のadapter。
時刻を制御した論理fixtureと、実時間の試験は別の証拠として記録する。
実時間では途中の要求と観測が残りのlease内で完了する環境が必要で、処理遅延を制御できない環境ではこの短いタイミング条件の適合判断を行わない。
reportのwaitは要求したmsであり、実時計の測定値ではない。時計・scheduler・経路の精度を検査済みとして扱わない。

`prepare_resource(call)`は、検査の所有SIDで代表資源を作るadapter。
`call(operation, payload=b'', fn=0)`は次のcorrで送信して生の応答を返す。core操作は名前、固有操作は整数を渡せる。
adapterは必要なfn/pin/操作を明示的に選び、結果を検証する。SID/corrを独自に進めず、このcallを使う。
`resources_released()`は対象の資源が無いことを独立に確認するpredicateで、prepare直後にはFalse、expiry/end/force後にはTrueを要求する。
両方を省略すればcore-onlyでも実行でき、資源解放はuncheckedへ残る。

coreの観測callbackは、再送前後の実行回数・期限更新・選択資源などだけを含める。待った分だけ増える時計はsnapshot比較へ含めない。
観測が無ければ応答と期限による選択検査だけで、再送/旧SIDの作用は未観測として残る。
不成立後は後続をnot_runとし、終了できなかったSIDとopenを試したSIDを報告する。自動的なforce/後始末は行わない。

長い操作中の期限、実行中のforceとqueue、実タイマー、全lease値/owner表示、transport障害はこの検査の範囲外。
reportは常にfull_conformance=false。テスト用raw fixtureのPASSを本番virtualや実機の適合と読み替えない。
