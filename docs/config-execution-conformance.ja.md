# save実行中の応答抑止とlease・応答完了

SPEC registryを明示してConfigExecutionModelとChecksを作り、InterfaceChecksの後に
`ConfigExecutionChecks(checks, SampleConfigExecutionAdapter(model)).run()`を実行する。
privateベンチや隣接SPECを自動探索しない。RegisteredOps2/context1/9ケース＝12項目、共通宣言33項目。
既存ConfigChecks8項目も別のadapterで検査し、get/set/unset/save/erase/stateを保つ。

保存先への実write呼出しで、fixtureの時計を1,500又は5,000ms進める。max_op_msは5,000ms。
実時計で待ったり実flashを書いたりするものではない。
実行途中にconfirm/list/describe/clock/lock_state/config get/stateの7要求を論理3経路から入力する。
返った値を呼出し側で観測し、21入力とも応答なしであることを確認する。
このfixtureは入力を捨てるが、一般Probeが後から処理するかどうかは定めない。

lease1,000/60,000msの4条件では、開始前に400msを消費する。
実行時間だけdeadlineを延ばし、保持者/plan/pinを保持する。応答bufferの準備でleaseを更新しない。
残りのleaseから1msを残して応答書込みを完了させ、完了時からleaseを数え直す。
次のlease−1/lease境界で保持と解放を確認する。
短いleaseの2条件では、応答を待つ599/600ms境界で期限切れになる。
後から旧tokenで応答を書き終えても所有者を復活させない。
完了済みsaveの同一要求再送は保存を再実行せず、期限切れセッションも更新しない。

同内容save、未知critical TLV、容量超過には実writeが無く、仮想実行時間も増えない。
拒否のraw tag/cause（cause TLV）と成功時の応答generationを確認し、これらの応答も生成時にleaseを更新し、送信完了では更新しない（core a09fa45 §6.1）。
各clock値は明示fixtureの制御条件で、一般Probeへ正確な実行時間を要求しない。
Checksが見るbuffer結果とmodelのprepared/sentイベントを区別する。
保存実行中は21入力の実戻り値を記録し、モデルが「抑止した」と記録するだけではPASSにしない。

新規43単体：正常1、共通宣言1、既存config操作1、wire結果故障9、結果tail切断7、時間/状態故障15、制御不足9。
lease停止漏れ/早い更新/待機中停止・更新/送信後更新漏れ、旧token復活、保存中応答/実行、
誤った実行時間、pin変更、同内容/拒否時/再送の書込みを検出する。
set/unsetは要求の先頭のcount(u8)から項目/行を読み、固定長に頼らない。
Host実時計のtimeout、物理flash、保存後のqueue処理、実経路の同時実行、応答待ち中のforce/open競合、
eraseの実行時間とproduction追従は残る。full_conformance=false。
