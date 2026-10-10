# optional stepと再停止の検査

`StepChecks`は選択SPECの`oep-if-debug.ja.md` §4/4.2とconsole §3を
第三段階`oep-interface`で検査する。`Checks(send, registry, unit)`と
`SampleStepAdapter(source)`を明示して`StepChecks(checks, adapter).run()`を呼ぶ。
全5 fnの共通宣言は`InterfaceChecks`の第二段階で確認する。
公開コードは設備や私的ベンチを探索しない。

前提1項目＋動作21項目。optional stepを提供する明示論理fixtureを使う。
提供しないprobeへstepの実装を要求するものではない。

- 成功時はstatus ok、PC before/afterとmovedを返す。自己分岐でPCが同じでも成功。
  u32 PCの上端から0への遷移も確認する。初めからstepが立っていても最後に下ろす。
- dcsr.prvは変更しない。prv 0/1/3を検査する。
- resumeは1回だけ。debugへ戻らない場合はhaltreqで再停止を試みる。
  再停止できたらstepを下ろしてstatus state。できなければhartは走ったまま、
  haltreqを下ろしてstatus stateと長さ0のstep_leftを返す。
  step_leftはstep bitが残りうる通知であり、残ることを必須条件にしない。
- statusがokでなければmovedと両PCは0。completed failedとの組合せを確認する。
  明示fixtureの準備中line/fault失敗ではresumeせず、PCを変えない。
- DATA1→DATA0の順で保存値を戻し、hartsel/haltreqを下ろす。
  abstractauto、無関係なGPR/memoryは変更しない。
- 同じconnectionのconsole pollをstep途中へ差し込み、scratchへ触れないことを確認する。
  成功・再停止・走行継続のいずれでもmailbox/frame/pendingを保持する。
  走行再開後は保存したdmseq出力を届ける。
- critical/壊れたTLV/切れた引数、session優先順、無効資源/stream/ARM資源を検査する。
  同一要求再送はstep/resumeを繰り返さず、保存結果をそのまま返す。

`StepModel`は既存block fixtureへstepを追加する独立論理モデル。
`step_seed(connection, pc, dcsr)`で保存前の状態を、
`step_behavior(mode, after_pc)`で結果を指定する。modeは
`ok` / `late-halt` / `running` / `prepare-line` / `prepare-fault`。
論理PC遷移と状態復元を扱い、CPU命令や抽象CSRコマンドを実行するものではない。
各caseは新bootとし、raw交換、PC/dcsr、DATA復元順、haltreqとconsole調停を記録する。

単体33件は19種類の誤動作注入、step_leftでstep bitが下がっていても通る正常例、
前提不足、不正刺激、共通宣言を含む。`full_conformance`はfalse。
物理命令/CSR操作、再停止の実時間、DATA復元自体の失敗、初めから走っているhartの受付、
実応答writer、reset/run、他targetとproduction Host統合は未検査。
