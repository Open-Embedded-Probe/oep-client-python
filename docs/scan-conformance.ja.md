# 標準wireの有限scan検査

`ScanChecks`は、選択SPECの`oep-if-debug.ja.md` §1/3/5を第三段階
`oep-interface`として検査する。scanは任意opなので、標準宣言検査では
欠落を失敗にしない。この検査の明示fixtureにはscanの提供を要求する。

`Checks(send, registry, unit)`と`SampleScanAdapter(source)`を明示して
`ScanChecks(checks, adapter).run()`を呼ぶ。公開コードは設備設定や私的ベンチを
探索しない。実装の版・SPEC identityと各検査のraw交換は既存の報告に残す。

前提1項目と動作20項目を検査する。

- RVSWD/SWIO/SWDの識別子、kind、候補順、triedとcount、未知応答TLV。
- 先頭から有限数を試し、残りをhostが次の要求で明示する。見つからなくても進む。
- RISC-V DMSTATUS.versionの0/1/2/3/14/15境界。SWDは読めたDPIDRを扱う。
- count 0、重複、切れた候補、未宣言の組、不正SWIO clock、使用中・disable・出力idleのpin。
  後ろの候補が不正でも、先頭へのアクセスを含め副作用前に拒否する。
- max_speedの下限・幅・raw critical tagのecho、未知critical/optional、最初のTLV採用。
- 各候補後のpin解放、資源・ID非生成、既存connection/consoleの保持、session判定、再送。

独立`ScanModel`は8 channel、明示候補、既定budget 2の論理fixture。
`scan_budget(1..3)`、`scan_id(fn, pair, u32-or-None)`、
`pin_mode(pin, disabled-or-output)`で刺激する。`scan_state()`は資源、pin予約・解放、
論理アクセスを返す。各caseは所有中には許さないlogical restartを使う。
これらは電気的な測定結果ではない。

単体では正常系、前提不足、無効な刺激に加え、tried/kind/pin/IDの破損、
資源生成、pin残留、候補飛ばし・暗黙追加、拒否前アクセス、budget無視、
無効versionの採用、再送結果破損の12種類を注入し、検査の検出力を確認する。

`full_conformance`はfalse。物理wake/dmactive/scratch/pin idle、idle_clock、
targetsel、電気的タイミング/max_op_ms、別frame上限・並行writer、production Host統合は
未検査として報告する。標準consoleのmailboxやtarget debug操作も別の検査である。
