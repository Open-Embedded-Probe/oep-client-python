# Probeのroleと要求見出し

[English](probe-input-conformance.md)

SPEC checkoutを明示し、`ProbeInputChecks(SampleProbeInputAdapter(), spec=path).run()`を実行する。
core/transports本文SHAとregistry待ち値を照合する。private設備・OS transportを使わない。
virtual Coreへraw messageを入力し、応答とowner/資源/cache/leaseを直接観測する。
fn1/op16はサンプル資源作成で、標準OEP固有操作ではない。


- DISCARD: roleが0x01以外の全255値を、10/11/64byteで入力する。既知のresult/event/dataも含め、無応答・状態非変更を確認する。
  role0x01の要求を0〜9byteに切る。見出し未完成のため、malformed応答すら返さずに捨てる。
- CONTROLS: 見出し10byte・短い固定payloadはmalformedで拒否し、正しい要求は実行する。捨てるだけのdummy backendを成功にしない。
- EXPIRY: 999msのdiscardで早く切れず、1000msのdiscard入力でも期限切れを実行する。旧履歴/番号を保持し、終了後の短い要求は無応答、正規旧SID要求はno_session。
- ENDED / EXPIRED: end後と期限切れ後に、それぞれDISCARDと同じroleと短い要求を入力し、無応答と全snapshot不変を確認する。
  endの新規履歴記録とexpiryの既存履歴保持を区別してseedを確認する。正常なSID0 clockを返し、旧SIDの新要求はno_sessionで断る対照も含む。

discard前後のsnapshotは同じ論理時刻で比較する。時計が期限へ進んだ際の資源解放は正しいtimer効果として区別する。
ownerは非空textをseedし、匿名所有者のままでは見逃すowner変更も検出する。
unowned時の応答/履歴破棄/last/allocator/generation変更とclock消失を検出する。
所有中だけ正しくroleを捨て、unowned時に別roleを実行する実Core故障も検出する。
Core._handleの`len(data) < 10 or data[0] != 1`による無応答predicateも、レビュー対象sourceとして対応監査へ記録する。
3つのmessage長の有限な入力検査を、全transport/frame上限/任意payloadの実送信試験とはしない。
物理framing・空transport frame、全拡張の依存資源、production Hostのrole処理と回復の結合は別範囲。
full_conformance=false。

```sh
uv run pytest -q tests/test_conformance_probe_input.py
```
