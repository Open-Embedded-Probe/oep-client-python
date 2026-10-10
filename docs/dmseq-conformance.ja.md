# probe側dmseq framingの検査

`DmseqChecks`は選択SPECの`target-console-dmseq.ja.md`のhost規則と
`oep-if-console.ja.md` §1〜3、`oep-if-debug.ja.md` §4.6を第三段階
`oep-interface`で検査する。ここでdmseq文書のhostはOEP probe側を指す。

`Checks(send, registry, unit)`と`SampleDmseqAdapter(source)`を明示して
`DmseqChecks(checks, adapter).run()`を呼ぶ。公開コードは設備や私的ベンチを探索しない。
adapterのmailbox word・poll・reset刺激と状態、各検査のraw交換を報告に保存する。

前提1項目＋動作28項目。選択したconsoleはmechanism 2を宣言することを前提にする。

- CRC-8の規範検査値FB/F3とword例3298/F300、payload長0〜6、DATA1を読む境界3を確認する。
  検査側のCRCは整数多項式の除算で計算し、モデル側のbit逐次計算を利用しない。
- N=7と全1 word、各CRC byteの8 bit破損、T=0の全128状態byteでは応答・同期変更・queue消費をしない。
- 未同期のS/Aの4組合せ、未同期時の未使用状態を0に固定せず、最初のSYN、通常の重複、SYNの重複を分けて検査する。
  同じSでも前の受理が非SYNなら新しいSYNで再同期する。新しいSYNは古いpendingを捨てる。
- 同じpayloadでもSが反転すれば新しい出力として受理する。TOのみでは再同期せず、通番の規則を適用する。
- pendingはackまで再送し、queueから二度取り出さない。重複target frameでもAがpendingのHをackすれば
  Hを反転して次の最大2 byteを載せる。空queueと再補充、容量4 byteの0〜5 byte writeを確認する。
- halt/debug実行中はmailboxへ触れず、再開時はDMSTATUSを先に読む。clearはpending/queue/同期を捨てない。
- 検出済みhaveresetの刺激はconnectionとbuffer/queueを保ち、dmseqを未同期へ戻し、restart/detail 1を付ける。
  markをOEPのmarks opで読み返す。reset直後に同じSのSYNが来ても新しく受理する。
- 二つのconnectionの同期・pending・queueは独立。closeは状態を解放し、新streamは新ID・未同期になる。
  write再送はlive状態もclose後もqueueや資源を再生成しない。

独立`DmseqModel`はSDI/DMDATAのfixtureへmechanism 2を追加する。状態は
synced/last_s/last_syn/h/pendingをstreamごとに持つ。`reset_target(connection)`は
検出済みtarget resetの論理刺激で、実機をresetしたりhaveresetを実際に観測したりしない。
各caseはlogical restartし、mailboxの全アクセスをpollごとのsnapshotで保存する。

単体23件は正常系・規範CRC例・前提不足と15種類の誤動作注入を含む。
`full_conformance`はfalse。target側の再送/timeoutアルゴリズム、resync/TO/framing markの条件、
実riscv-dm要求との調停、物理DMI/timing/配送、production Host統合、別容量と並行pollerは未検査。
probe側のframing検査だけでtarget側や全consoleの適合とは扱わない。
