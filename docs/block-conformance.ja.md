# optional block操作と状態復元の検査

`BlockChecks`は選択SPECの`oep-if-debug.ja.md` §4/4.5とcore §7.4を
第三段階`oep-interface`で検査する。`Checks(send, registry, unit)`と
`SampleBlockAdapter(source)`を明示して`BlockChecks(checks, adapter).run()`を呼ぶ。
公開コードは設備や私的ベンチを探索しない。共通宣言は`InterfaceChecks`で第二段階に分ける。

明示fixtureではread_block/write_blockを組で提供し、
max_length=8 byteを宣言する。この値を他のprobeへ要求しない。

- max_lengthの単位/幅、opの組、選択経路のframeへの収容とcountの上限を確認する。
- addressの4 byte alignment、count超過、切れたwrite dataは副作用前に拒否する。
- count=0はhartが止まっている/走っている両方でsuccess / done 0。
  count>0でhartが走っていればstatus state / completed failed、done 0。
- 実行済みprefixのdoneとoutcome、read値の順序、write失敗後のsuffix非実行。
  target memoryを外部刺激で変更した後の新しいreadを検査し、probe側の古い写しを使わない。
- 成功/途中失敗とも、GPR s0/s1/a0/a1とDATA1/DATA0/abstractautoを触る前の値へ戻す。
  unrelated GPRは変えない。DATA1をDATA0より先に戻し、高水準opのhartselは0で返す。
- 同じconnectionのconsoleをblock中にpollしてもscratchへ触れない。
  生きたdmseq frameを保存・復元し、resume後に元の出力が届くことを確認する。
- read/writeの同一要求再送はbusアクセスを繰り返さず、結果を計算し直さない。
- DATAの復元（後始末）に失敗したら、全語を転送してもcompleted partial（done>0）でstatusは失敗の値。
  応答の後でconnectionとそのstreamを閉じ、次の要求はno_resource（debug §4）。

独立`BlockModel`はmandatory debug fixtureへoptional opを追加する。
`block_memory(connection, aligned-u32-to-u32-dict)`はtarget busの論理刺激、
`block_scratch(connection, gpr-to-u32-dict)`は保存前のGPR、
`block_failure(index-or-None, status)`は語の直前の失敗を、`block_restore_failure(bool)`は後始末の失敗を指定する。
状態にはbusのaccess、復元順、memory/GPR/mailbox/pendingを保存し、各caseを新bootにする。
物理abstract command、program bufferやsysbusの実装ではない。

単体28件は17種類の誤動作注入、前提不足、不正刺激、全5 fnの共通宣言を含む。
`full_conformance`はfalse。全transport経路のframe制約、物理GPR復元失敗、
abstract command/program buffer/sysbusのアルゴリズム、RV32番地overflowとbusアクセス権、
reset/step/run、実応答writer、別容量とproduction Host統合は未検査。
外部memory刺激の検査だけでdmi/runによる実target memory変更まで証明したとは扱わない。
