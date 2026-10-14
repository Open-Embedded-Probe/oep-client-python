# 同内容saveの書込み抑止と保存容量の境界

SPEC registryを明示したChecksとConfigStorageModelを作り、InterfaceChecksの後に
`ConfigStorageChecks(checks, SampleConfigStorageAdapter(model)).run()`を実行する。
privateベンチ/隣接SPECを自動探索しない。RegisteredOps2/context1/7ケース＝10項目、共通宣言33項目。
保存容量64byte、items label/idle/disableの明示profile。

保存先はRamConfigStorage。writeの実呼出しで独立copyをcommitし、その回数と保存内容を観測する。
write_countは論理設定全体のcommit回数であり、一般のflashが何回の物理writeで保存するかを定めない。
初回のsaveは保存し、同内容を再度saveしてもwriteを呼ばない。
同一要求の再送と新しいcorrのsave、再起動後の別セッションのsaveを区別する。
一度set/unsetで変更して保存せず元へ戻した場合、generationは保存時から3進んでいるが、setの順やbit7が違っても保存内容が同じなら書かない。
保存の判定をgenerationの一致だけに頼らないことの確認であり、saveの後はsaved_generationを現在のgenerationへ置く。
変更した内容をsaveしたときは、新しい内容へ置換し、次の同内容saveは書かない。
eraseは保存先を消してstate 0/0/0とし、現在設定を保持する。その後は同じ内容でも新たに保存する。
save/eraseはlive GPIO/planを動かさず、bootで保存先を書き換えない。再起動後はgeneration 1、state 1/1/0。

容量63/64/65byteはlabel/idle/disableの設定で検査する。
計算はlabel37＋label(5＋text長10/11/12)＋idle6＋disable5。
max_bytesが数えるのは項目の見出し込みのbyteだけなので、63/64は保存でき、65はcause 3で拒否する。
65の拒否では現在設定、generation、以前の保存とsaved_generation、live資源、backendのcommit回数/内容を保持する。
次のbootは以前の保存へ戻る。63/64は新しい保存へ復帰する。

単体33件：正常1、共通宣言1、wire故障7、意味的故障18、制御不足6。
毎回書込み/書込み欠落/変更の取りこぼし、generationや挿入順だけの比較、set時の自動保存、
erase漏れ/現在設定消去/state残留、容量境界/cause/拒否時の書込み、pin変更、不正保存、boot時の書換え/消失を検出する。
回数のassertは明示backendの観測条件であり、保存の内部形式や物理保存方式をoracleとして用いない。
実flashの摩耗、保存中排他/電源断、起動時の項目ごとの検証、production Host統合は残る。
full_conformance=false。
