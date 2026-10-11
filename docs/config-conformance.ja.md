# 現仕様のconfig操作とRAM保存の部分検査

ConfigModelは現在のCore/GPIO/planへconfigを接続し、ConfigChecksはそのwire操作を検査する。
SPEC registryを明示してChecksを作り、InterfaceChecksの後に
`ConfigChecks(checks, SampleConfigAdapter(model)).run()`を実行する。private設備や隣接SPECは探索しない。
RegisteredOps2/context1/4ケース、合計7項目。storage64 byte、items plan/label/idle/disable、slots_max0の明示profile。
save/eraseは組で宣言する。保存はRAM内の独立なcopyであり、flash耐久性や電源断を実装するものではない。

- PERSISTENCE：set/get/hash、明示save、未保存変更のstorage_hash非更新、再起動で保存設定へ復帰、erase後も現在の設定/pinを保持、次の再起動では設定なし。
- PRESET：保存用plan設定とunsetはlive plan/pinを変更しない。所有中pinへのidle変更は次に解放した際に適用する。
- ATOMIC：重複キー、idle/disable競合、短い値、未知項目は設定/保存/資源を変えず拒否。idleを外した後の使用中pinのdisableはcause1。複数の拒否条件の優先順位を新たに定めない。
- STORAGE-LIMIT：現在の設定が保存容量を超えてもsetは可能。saveをcause3で拒否し、現在/保存を維持する。

getの項目はcritical bitを外し、hashはProbeが作る値として比較する。HostはCRC等を計算しない。
model内のhashとserializationは検査oracleではない。操作の返り値、get/state、実際のGPIO/planを独立に照合する。
この基本検査のfn/role/channelは固定。保存先のinterface番号が変わる条件や読めない保存のfallbackは、
別の[保存復帰検査](config-fallback-conformance.ja.md)で確認する。

`uv run pytest -q tests/test_conformance_config.py`は20件。
正常1、共通宣言1、wire故障4、意味的故障11、制御不足3。
新モデルのeraseが現在設定も消す違反とstorage fullのcause4を再現し、eraseは保存だけ/cause3へ修正した。
旧config consumer/Endpointの自動attach・bind形は、この現仕様の適合根拠には使わない。
get paging/数値キー順は[別の部分検査](config-paging-conformance.ja.md)で確認する。
同内容saveの書き込み抑止と保存容量境界は[別の部分検査](config-storage-conformance.ja.md)で確認する。
全項目、電源断/保存実行中の排他、
slot/uartのidentity読み替え、wifi、target、物理保存とproduction追従は残る。full_conformance=false。
