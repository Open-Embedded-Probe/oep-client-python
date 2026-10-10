# optional runとloader状態の検査

`RunChecks`は選択SPECの`oep-if-debug.ja.md` §4/4.4とconsole §3を
第三段階`oep-interface`で検査する。`Checks(send, registry, unit)`と
`SampleRunAdapter(source)`を明示して`RunChecks(checks, adapter).run()`を呼ぶ。
全5 fnの共通宣言は第二段階の`InterfaceChecks`へ分ける。
公開コードは設備や私的ベンチを探索しない。

前提1＋動作28項目。optional runを提供する明示論理fixtureを使う。
runはloader呼出しであり、デバッガのcontinueへ流用しない。

- 正常停止はstopped 1 / success。PCが開始位置のままでも成功にし、resumeを出し直さない。
- 時間切れで停止できればstopped 0 / status timeout / completed failed。
  dpcと要求された値を有効として返す。停止できなければstopped 2、dpc=0、nvals=0。
- 準備中line/fault失敗はstopped 3、dpc=0、nvals=0、elapsed_us=0。
  hartを走らせず、止まったまま返す。
- elapsed_usはresumereqから停止観測または停止を諦めるまでの時間。
  準備時間を足さず、時間切れ後の停止待ちを含める。
  fixtureは準備400 us、正常停止100 us、timeout_msに追加して停止待ち200/300 usを使う。
  実時計での正確さや別probeへこの時間を要求するものではない。
- host指定のPC/GPRとdcsr.ebreakm/prv Mは変えたまま返す。
  DATA1/DATA0/abstractautoは保存値へ戻し、hartsel/haltreqを下ろす。
  fixtureはDATA/abstractautoをresume前に復元する。物理abstract read/writeは扱わない。
- 0入力/0出力、同じregnoの出力を複数返す順序、u32 PC、timeout=0を確認する。
  timeout_ms > max_op_msは副作用前にunsupportedで拒否する。
- 数と合わない入力/出力配列、切れた引数、TLVの壊れとcritical、session/資源を事前検査する。
  n_outの結果が選択経路のmax_frameぴったりの例と、超える例を確認する。
  必須固定欄と値が収まらなければunsupported。任意の応答TLVのために上限を超えない。
- 論理programが変更したmemory/GPRは保持し、戻り値と後続read_blockへ反映する。
  同connectionのconsole pollはrun中にscratchへ触れず、mailbox/pending/buffer/marksを保つ。
  別connectionを変えない。応答後にconsoleを再開する。
- 正常停止/停止させた時間切れ/走行継続の再送は保存結果を返し、loaderを二度実行しない。

`RunModel`は既存reset fixtureへrunを追加する独立論理モデル。
`run_behavior(mode, after_pc)`で`stopped` / `timeout-halted` / `not-halted` /
`prepare-line` / `prepare-fault`を選ぶ。
`run_program(connection, memory, gprs)`は実行後の論理program効果を明示する。
memoryはaligned-u32 address → u32 value、gprsはindex 0〜31 → u32 value。
今回の論理runは抽象GPR番号0x1000〜0x101Fだけを扱う。この範囲を標準仕様の上限にしない。
CPU命令、CSR/抽象コマンド、実clock、実targetへの転送を実行しない。
各caseを新bootとし、raw交換、resume/finish時刻、PC/dcsr/GPR、復元とconsole調停を記録する。

単体47件は28種類の誤動作注入、前提不足、不正刺激と共通宣言を含む。
`full_conformance`はfalse。全transport経路のframe上限、未対応抽象reg/部分準備の方針、
初期走行hart受付、結果読出し/DATA復元自体の失敗、programが実行中にmailboxへ書く場合、
物理CSR/CPU/clock、実応答writer、他target/容量とproduction Host統合は未検査。
