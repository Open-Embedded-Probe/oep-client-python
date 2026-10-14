# 標準consoleのSDI/DMDATA mailbox検査

`MailboxChecks`は選択SPECの`oep-if-console.ja.md` §1〜3を第三段階
`oep-interface`で検査する。`Checks(send, registry, unit)`と
`SampleMailboxAdapter(source)`を明示して`MailboxChecks(checks, adapter).run()`を呼ぶ。
私的ベンチや設備設定を公開コードから探索しない。raw交換・刺激・DMIアクセス記録を報告に残す。

前提1項目＋動作22項目。対象はmechanism 0/1を明示提供する論理sampleである。

- SDIはL=0〜7を検査し、L=1〜7ではDATA0→DATA1読出し→DATA0=0の順で受信する。L≥8は枠でないのでDATA0=0で消す（console §3.1）。
  L=8〜255では受信も消去もしない。短い枠でもDATA1を読む。
- SDIには送信列がない。SDI streamへのwriteはゼロ長を含めunsupported（payload 0）。
- DMDATAはT=0の状態byte全128値で無操作。T=1ではL=4の空枠、L=5〜11の受信、
  L=0〜3/12〜63の消去を検査する。bit 6は無視する。DATA1は受信が4 byte以上の場合だけ読む。
- 有効target枠にはDATA0をちょうど1回書き、DATA1は書かない。送りの列から最大3 byteの先頭を
  消費し、空なら0で答える。不正枠は0で消去し、送りの列を消費しない。
- 容量4 byteのwriteで全受理・部分受理・満杯・ゼロ長・空き復帰と順序を検査する。
  acceptedはqueueへの受付を意味し、targetへの配送確認ではない。
- halt中はDATA0/DATA1へアクセスしない。debug要求実行中はpollを停止する。
  再開時はDATA0より先にDMSTATUSを読む。停止中にqueueや受信bufferを変えない。
- readは非消費、clearは受信bufferだけを捨てる。writeの再送はqueueを再補充しない。ack後の同じ枠を再配送しない。

独立`MailboxModel`は既存attach sampleを土台にmailboxを追加する。
`mailbox_frame(stream, data0, data1)`でu32の論理wordを置き、`mailbox_poll(stream)`を明示実行する。
`mailbox_halted(stream, bool)`、`mailbox_debug_busy(stream, bool)`は外部状態の刺激であり、
実際のtarget操作ではない。DMSTATUSのanyhaltedを読み、DATA0/DATA1のアクセスを記録する。
状態snapshotのeventsは直近のpollだけで、各pollのsnapshotをadapter traceへ保存する。
各caseは新bootでlogical restartする。実機やファームウェアを操作しない。

単体23件には15種類の誤動作注入を含む。DATA1の欠落/過剰読出し/書込み、不正枠消去の違反、
自己応答の上書き、queueの誤消費/逆順/短縮、halt/debug中アクセス、DMSTATUS順序違反、
SDI writeの受理、bit 6の誤解釈、ack未反映を独立検査で検出する。

`full_conformance`はfalse。dmseq、target自己reset/restart mark、実際の標準riscv-dm要求との
arbitration、物理DMI/timing/配送、production Host統合、別queue/ring容量と並行pollerは未検査。
このsampleの状態switchだけで、標準debug要求のwire実装まで検証したとはしない。
