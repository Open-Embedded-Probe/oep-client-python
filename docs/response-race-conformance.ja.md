# 応答待ちとforceの競合

SPEC checkoutを明示し、`ResponseRaceChecks(SampleResponseRaceAdapter(), spec=path).run()`を実行する。
core/transports本文SHAとregistry待ち値を照合する。private設備・OS transportを使わない。
現在のCore.handle(defer_send=True)とresponse_sentを、3本の論理writerと明示millisecond時計で通す。
fn1/op16は資源を作るサンプル拡張で、標準OEP操作ではない。

13項目、12条件：6種類×lease1000/60000。

- FORCE：旧所有者7の成功応答が待つ間に8がforceし、新資源を作る。後で旧応答を完了しても新lease/資源/cacheを変えない。
- REJECTED：旧所有者の要求が拒否された応答の場合も、旧完了通知が新しい所有者を更新しない。
- DOUBLE-FORCE：7→8→9とforceし、7と8の応答を遅く完了する。どちらも9へ影響しない。
- EXPIRE-NEW：8の新leaseを正確な期限で終了させてから7の応答を完了する。資源・ロックを復活させない。
- NO-FORCE：8のopenをlockedで拒否する。その拒否応答は7のleaseを更新せず、7自身の応答完了だけが期限を更新する。再送は更新しない。
- RETURN-ID：raw Probe刺激として7→8→7とforceする。内部の所有期間が違うため、古い7の完了通知を新しい7へ適用しない。Hostは終了したsession_idを再使用してはならず、この条件をHost使用例とはしない。旧wire要求は再送しない。

実request/reply byte、完了token、時刻、Core owner/generation/lease、資源番号とlive状態、cacheを保存する。
forceは旧資源を解放してから新しいownerを立て、資源番号は同bootで再利用しない。
force後の旧wire要求はlocked、後続期限切れ後はno_session。旧待ち応答は処理時の結果を保持する。
token/generationは現Coreの内部実現方法の観測で、新しいwireフィールドを定義しない。
契約は旧応答による後続所有期間の非変更で、別のProbeに同じtoken表現を要求しない。

単体27件：正常1、観測故障20、実Core/資源故障4、制御不足2。
corr/結果/token、早い更新、資源・cache混入、force拒否、新lease期限、同IDの所有期間混同を検出する。
初期のcorr故障は検査のlookupでStopIterationになったため、順序から旧待ち要求を選んで独立にbyte照合するよう直した。
現在のCore/資源モデル/productionは変更していない。既存writer/lease28件も含め55件が通過する。
論理順序を定めた競合で、実CPU同時実行・物理transport完了・全拡張資源の依存解放・production Host統合は別の検査。
full_conformance=false。
