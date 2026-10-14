# optional resetとDM復帰の検査

`ResetChecks`は選択SPECの`oep-if-debug.ja.md` §4/4.3/4.6、console §2/3、
共通mark §1を第三段階`oep-interface`で検査する。
`Checks(send, registry, unit)`と`SampleResetAdapter(source)`を明示して
`ResetChecks(checks, adapter).run()`を呼ぶ。全5 fnの共通宣言は第二段階の
`InterfaceChecks`へ分ける。公開コードは設備や私的ベンチを探索しない。

前提1＋動作24項目。optional resetを提供する明示論理fixtureを使う。
resetを提供しないprobeへ実装を要求するものではない。

- modeは0と2だけ（debug §4.3）。1と3はunsupported（固定部分の印0）。
  flagsはbit0 reachedだけで、ほかのbitは0。successはreachedが立ち、後始末も済んだ場合だけ。
  mode 0のPCは0、mode 2はdpcを返す。
- 2 modeの成功、状態到達timeout、DM line失敗、cmderr fault、後始末の失敗を区別する。
  失敗もcompletedと同じ固定応答の形を保つ。後始末（haltreqを下ろす）に失敗した場合は
  flags reachedのままstatus lineのfailedで答え、応答の後でconnectionとそのstreamを閉じる。
- ndmresetを立てて解く。mode 2は解除時にhaltreqを保ち、応答前に下ろす（hartは止めたまま）。
  mode 0も応答前に下ろす。hartselとndmresetは応答前に0にし、reset pinを動かさない。
- ndmreset解除後のDMSTATUSがlineなら待って読み直す。論理fixtureはmax_op_ms=1、
  200 us間隔で最大5回読み、2回line後に復帰する例と最後までlineの例を持つ。
  読みは全体上限より前に行い、DMが戻らなくてもconnection/streamを保つ。
  この間隔/回数を他probeへ要求しない。wire wakeの再送は任意なので必須条件にしない。
- DMが応えたらhaveresetを確認応答。各streamのdmseqを未同期へ戻し、pendingを捨てる。
  生きたbuffer/position/送りqueueは保つ。reset mark（detail 1 ndmreset）と
  restart mark（detail 1 havereset）を区別し、wire marks読出しでも確認する
  （markの種類と付け方はconsoleの段で今の仕様へ合わせる。今は従来の番号）。
  DMが戻らずhavereset未観測ならrestartを付けない。未観測時のdmseq同期解除は合否条件にしない。
- 同じconnectionのconsole pollはreset中にscratchへ触れず、
  別connectionのtarget/mailbox/protocol/streamへ影響を与えない。
- 不明mode、critical/壊れたTLV/切れた引数、sessionと資源拒否は副作用前に行う。
  成功/DM line失敗の同一要求再送は、reset・mark・ack・確認を繰り返さない。

`ResetModel`は既存step fixtureへresetを追加する独立論理モデル。
`reset_behavior(result)`で`ok` / `state-timeout` / `dm-recover` / `dm-line` /
`fault` / `cleanup-line`を選ぶ。
論理状態・順序・PCを明示し、物理DMやCPU命令を実行するものではない。
各caseを新bootとし、raw交換、ndmreset/DM read/ack/復元、buffer/mark/pendingを記録する。

単体40件は24種類の誤動作注入、DM未復帰でも予防的に同期を解除する正常例、
前提不足、不正刺激、共通宣言を含む。`full_conformance`はfalse。
物理ndmreset/wakeとDM復帰時間、havereset ack自体の失敗、
実応答writer、run、別target/容量とproduction Host統合は未検査。
