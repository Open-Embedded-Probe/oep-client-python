# 同内容saveの書込み抑止と保存容量の境界

SPEC registryを明示したChecksとConfigStorageModelを作り、InterfaceChecksの後に
`ConfigStorageChecks(checks, SampleConfigStorageAdapter(model)).run()`を実行する。
privateベンチ/隣接SPECを自動探索しない。RegisteredOps2/context1/9ケース＝12項目、共通宣言33項目。
保存容量64byte、items plan/label/idle/disableの明示profile。

保存先はRamConfigStorage。writeの実呼出しで独立copyをcommitし、その回数と保存内容を観測する。
write_countは論理設定全体のcommit回数であり、一般のflashが何回の物理writeで保存するかを定めない。
初回のsaveは保存し、同内容を再度saveしてもwriteを呼ばない。
同一要求の再送と新しいcorrのsave、再起動後の別セッションのsaveを区別する。
一度setで変更して保存せず元へ戻した場合、setの順やcritical bitが違っても保存内容が同じなら書かない。
変更した内容をsaveしたときは、新しい内容へ置換し、次の同内容saveは書かない。
eraseは保存先を消し、現在設定を保持する。その後は同じ内容でも新たに保存する。
save/eraseはlive GPIO/planを動かさず、bootで保存先を書き換えない。

保存の同一性はfnの数値やget hashだけで判定しない。
GPIO instance0/1のfn2/3入替え後はwireのfn/hashが変わっても、同じ(name,instance,revision)への設定なので書かない。
保存の識別子表と内部fnを保持し、stateのstorage_hashは現在のget hashと一致する。
同じfnのrevisionだけが変わった場合は、旧保存の復帰をreason2で拒否する。
hostが同じwire項目を再設定してsaveすると、新しいrevisionを保存し直す。
この例ではget hashは同じでも、指すinterfaceの組が違うため書込みが必要になる。

容量63/64/65byteはplanの識別子表を持つ設定で検査する。
計算はplan8＋label37＋label(5＋text長6/7/8)＋idle7。
max_bytesが数えるのは項目TLVだけなので、63/64は保存でき、65はcause3で拒否する。
65の拒否では現在設定、以前の保存/hash、live資源、backendのcommit回数/内容を保持する。
次のbootは以前の保存へ戻る。63/64は新しい保存へ復帰する。

単体36件：正常1、共通宣言1、wire故障9、意味的故障19、制御不足6。
毎回書込み/書込み欠落/変更の取りこぼし、raw fn/hash/挿入順だけの比較、識別子無視、set時の自動保存、
erase漏れ/現在設定消去、容量境界/cause/拒否時の書込み、pin変更、不正保存、boot時の書換え/消失を検出する。
回数のassertは明示backendの観測条件であり、Hashアルゴリズムや物理保存方式をoracleとして用いない。
実flashの摩耗、保存中排他/電源断、全項目/slot-uart参照、任意カタログ、production Host統合は残る。
full_conformance=false。
