# Probeのroleと要求見出し

SPEC checkoutを明示し、`ProbeInputChecks(SampleProbeInputAdapter(), spec=path).run()`を実行する。
core/transports本文SHAとregistry待ち値を照合する。private設備・OS transportを使わない。
現在のvirtual Coreへraw messageを入力し、応答とowner/資源/cache/leaseを直接観測する。
fn1/op16はサンプル資源作成で、標準OEP固有操作ではない。

4項目、781観測。

- roleが0x01以外の全255値を、10/11/64byteで入力する（765件）。既知のresult/event/dataも含め、無応答・状態非変更を確認する。
- role0x01の要求を0〜9byteに切る（10件）。見出し未完成のため、malformed応答すら返さずに捨てる。
- 対照2件：見出し10byte・短い固定payloadはmalformedで拒否し、正しい要求は実行する。捨てるだけのdummy backendを成功にしない。
- lease境界4件：999msのdiscardで早く切れず、1000msのdiscard入力でも期限切れを実行する。旧履歴/番号を保持し、終了後の短い要求は無応答、正規旧SID要求はno_session。

discard前後のsnapshotは同じ論理時刻で比較する。時計が期限へ進んだ際の資源解放は正しいtimer効果として区別する。
ownerは非空textをseedし、匿名所有者のままでは見逃すowner変更も検出する。
単体23件：正常1、観測故障13、実Core故障4、制御不足5。現在のモデル本体/Core/productionに修正はない。
Core._handleの`len(data) < 10 or data[0] != 1`による無応答predicateも、レビュー対象sourceとして対応監査へ記録する。
3つのmessage長の有限な入力検査を、全transport/frame上限/任意payloadの実送信試験とはしない。
物理framing・空transport frame、全拡張の依存資源、production Hostのrole処理と回復の結合は別範囲。
full_conformance=false。
