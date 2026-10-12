# 同時に不正な条件がある要求の拒否順序

SPEC checkoutを明示し、`RefusalOrderChecks(SampleRefusalOrderAdapter(), spec=path).run()`を実行する。
core/transports本文SHAとregistry待ち値を照合する。private設備・実transportを使わない。
現在のCoreとサンプル資源拡張へraw要求を送り、core §4.3の順序を確認する。

11項目、60観測。所有中/期限切れと、通常保持/要求保持不足/応答保持不足の6状態へ、10種類の要求を入力する。
実remember_maxを2048/10/16にして、要求だけ又は応答だけを保持できない状況を作る。cacheを偽造して境界を作らない。

- corr0、未知fn、未知op、SID0でのsession_requiredは、再送・所有・payload条件より先。
- 一致した再送は保存した応答を返す。期限切れでも履歴が残る。保持不足はresult_lost。
- 保持した要求と違うbyteはmalformed。応答を失っていても、保持した要求との一致性を先に確認する。
- 未記録のcorrが過去の最大以下ならresult_lost。現在所有やpayloadの解釈より先。
- 外国SIDは、payloadも不正でもlocked/no_sessionで断る。
- 最後の段階に不正kind・未知critical TLV・明示admission拒否が同時にあれば、該当する理由のどれかでよい。malformed/unsupported/window_exceededへ余計な固定順位を加えない。

早い見出し拒否・再送・外国SID拒否はowner/資源/cache/leaseを変えない。
有効所有者の新しい拒否応答は、資源を変えずにleaseと履歴を更新する。
期限切れ後の最後のSIDへの新しいno_sessionも履歴へ記録するが、leaseや資源を復活させない。
この履歴更新を「拒否に副作用がある不具合」と混同しない。
admission_reason=6は現Coreの明示モデルhookで、物理buffer容量の証明ではない。

単体32件：正常1、誤理由10、状態/保持故障10、最後の段階の許容理由3、実Core順序故障4、制御不足4。
新規拒否の記録で旧履歴を壊さず、保持上限に応じたrequest/result byteを記録することも照合する。cache観測は正規化したdebug情報で、wire形式ではない。
初期検査で終了した最後のSIDの新規拒否履歴更新を見落としていたため、検査側を修正した。Core/モデル本体は不変。
全拡張のpayload/資源状態の組合せ、実admission・transport・production Host統合は別途検査する。
full_conformance=false。
