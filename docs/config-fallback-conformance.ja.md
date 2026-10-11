# 保存interfaceの読み替えと設定全体のfallback

SPEC registryを明示してConfigModelとChecksを作り、InterfaceChecksの後に
`ConfigFallbackChecks(checks, SampleConfigFallbackAdapter(model)).run()`を実行する。
公開側はprivateベンチや隣接SPECを探索しない。保存/カタログ変更は明示adapterのRAM制御に限定する。
RegisteredOps2/context1/8ケース＝11項目。共通宣言は33項目。

保存presetのfnを(name, instance, revision)で記録し、次のbootのlistと照合する。
GPIO instance 0/1のfn 2/3を入れ替えて、getのpresetがfn 3を指すことと、
stateのstorage_hashが現在のget hashを返すことを確認する。
参照していないinstance 1のrevision変更は保存設定を無効にしない。
どちらもpresetを実行せず、idleだけを反映する。次のbootでも同じ読み替えになり、保存識別子を書き換えない。

復帰できないケースでは現在設定を空に保ち、storage_state 2/storage_hash 0を返す。

- FORMAT：保存の値の長さが壊れた場合はreason 1。単体ではUTF-8、plan長さ/キー、識別子表の欠落/形/余分な参照も扱う。
- MISSING/NAME/INSTANCE/REVISION：保存が参照する組がlistに無い場合はreason 2。
- APPLY：個々の項目は読めるが同じchannelのidle/disableが競合し、設定全体を適用できない場合はreason 3。

getに一部の項目だけを残さず、pinを駆動せず、planを作らない。
保存disableのchannel 6へ別の健全なGPIO instanceからplan/setできることも確認し、
getには出ないdisableだけが残る故障を検出する。
通常操作と2回目のbootでも読めない保存自体を保持する。
3ケースはeraseから再設定、3ケースは読めない保存を維持したままsetし、明示saveで置き換える。
どちらも次のbootで修復済み設定へ復帰する。

hashの作り方やRAM保存の内部形は規範ではなく、oracleはwire項目とget/state/saveの関係を照合する。
壊れた保存の形はfixtureが選んだ例であり、一般Probeのstorage serializationを定めない。
複数の復帰失敗理由を重ねた場合の優先順位は検査しない。

単体は37件：正常1、共通宣言1、wire故障8、意味的故障15、制御不足6、破損形6。
stateの誤った理由/状態/hash、idle/disable/項目の部分適用、保存の勝手な書換え、旧fnの使用、
preset実行、無関係な変更での復帰拒否、新boot欠落、set/save/erase後の誤った保存状態を検出する。
get pagingは[別の部分検査](config-paging-conformance.ja.md)で確認する。
同内容saveの書込み抑止は[別の部分検査](config-storage-conformance.ja.md)で確認する。
slot/uartの参照、任意カタログ、全項目、保存中排他/電源断、
物理保存とproduction追従は残る。full_conformance=false。
