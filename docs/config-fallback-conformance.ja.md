# 保存項目の起動時検証と部分適用

SPEC registryを明示してConfigModelとChecksを作り、InterfaceChecksの後に
`ConfigFallbackChecks(checks, SampleConfigFallbackAdapter(model)).run()`を実行する。
公開側はprivateベンチや隣接SPECを探索しない。保存内容の変更は明示adapterのRAM制御に限定する。
RegisteredOps2/context1/8ケース＝11項目。共通宣言は33項目。

label ch4、idle ch1 output-high、disable ch6をwireでset/saveした後、adapterが保存内容を古いfirmwareや破れた書込みが残しうる形へ変える。
次のbootでは保存項目を1つずつ検証し、disable、idle、labelの順に適用する。
getの項目とgeneration 1、stateのstorage_state/saved_generation 0/reason、pin（disableは非駆動、ほかはidle、無ければHi-Z）、
ch1/ch6へのplanの可否（disableならunavailable cause 5）を照合する。

- FORMAT：容器が壊れて境界を確かめられない。何も適用せずstate 2/reason 1。保存のdisable ch6も効かない。
- UNKNOWN：未知の項目（旧planの0x01）。それを除き適用し、state 3/reason 2。
- CHANNEL：channels以上のlabel。MODE：未知のidle mode。LABEL：textの無いlabel。いずれも除いて適用し、3/2。
- CONFLICT：同じchannelにidleとdisable。disableを優先してidleを適用せず、3/2。
- DUPLICATE：同じキーのlabelが2つ。そのキーを適用せず、3/2。
- CAPACITY：保持量を3項目へ減らし、labelを1つ加えた4項目の保存。disable、idle、labelのchannel順に選び、label ch4を落として3/3。

不正な項目を別channelや別modeへ読み替えない。通常操作と2回目のbootでも保存自体を書き換えず、同じ結果になる。
UNKNOWN/DUPLICATEはeraseでstate 0/0/0にしてから、ほかはstateを保ったままsetし、明示saveで置き換える。
setだけでは保存とstateを変えない。saveの後はstate 1/現在のgeneration、次のbootで修復済み設定とstate 1/1/0へ復帰する。

保存の内部形はfixtureが選んだ例であり、oracleはwire項目とget/state/saveの関係を照合する。
壊れた容器の形も一般Probeのstorage serializationを定めない。
複数の理由を重ねた場合のreasonの優先順位は検査しない。idle/disableの併存をreason 2とするのはこのfixtureの選択で、§3は理由を明記しない。

単体41件：正常1、wire故障8、意味的故障20、制御不足7、共通宣言1、破損形4。
stateの誤った理由/状態/saved_generation、破損時の部分適用・disableの残留、有効な項目の全拒否、不正項目の読替え/残留、
idleの優先、重複キーの片方の採用、容量不足時の順序、起動時generation、保存の勝手な書換え、新boot欠落、
set/save/erase後の誤った保存状態、idleの未適用を検出する。
get pagingは[別の部分検査](config-paging-conformance.ja.md)で確認する。
同内容saveの書込み抑止は[別の部分検査](config-storage-conformance.ja.md)で確認する。
erase中の電源断で残る完全性を確認できない保存、保存中排他/電源断、物理保存とproduction追従は残る。full_conformance=false。
