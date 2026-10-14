# 現仕様のconfig操作とRAM保存の部分検査

ConfigModelは現在のCore/GPIO/planへconfigを接続し、ConfigChecksはそのwire操作を検査する。
SPEC registryを明示してChecksを作り、InterfaceChecksの後に
`ConfigChecks(checks, SampleConfigAdapter(model)).run()`を実行する。private設備や隣接SPECは探索しない。
RegisteredOps2/context1/5ケース、合計8項目。storage max_bytes 64、items label/idle/disable（0x02/0x03/0x07）、channels 8の明示profile。
save/eraseは組で宣言する。保存はRAM内の独立なcopyであり、flash耐久性や電源断を実装するものではない。

要求の形はset `count(u8) 項目 [TLV]`、unset `count(u8) count×(tag(u8) channel(u16)) [TLV]`、
getの応答は`more generation count 項目 [TLV]`、stateは`storage_state saved_generation reason`。
項目はtag(u8) len(u16) valueで、応答の末尾TLVとは別に数えて読む。

- PERSISTENCE：起動時のgeneration 1、bit7付きの項目tagを無視したset、初めての変更で2、同じ内容のsetはgenerationを変えない。
  saveの応答とstate 1/現在のgeneration、保存後の変更でもsaved_generationを保つこと、再起動で保存設定とgeneration 1・state 1/1/0へ復帰、
  eraseは現在の設定/generation/pinを保ってstate 0/0/0、次の再起動では設定なし。
- IDLE：使用中のchannelへのidleは解放時に効き、空きのchannelへのidleはただちに効く。
  disableしたchannelは駆動せず、planはunavailable cause 5で断る。disableのunsetでただちに空き状態へ戻り、使えるようになる。
- ATOMIC：重複キー（bit7違いを含む）、idle/disable/labelの長さ違い、項目番号0、countと項目の不一致はmalformed。
  宣言しない項目（旧planの0x01、0xFF）は受け取ったtagで、範囲外channel・未知のidle modeは項目tagで、未知criticalの要求TLVはそのtagでunsupported。
  unsetの重複キー/項目番号0/切れた行はmalformed、宣言しない項目はunsupported。
  idleとdisableの併存はcause 5、使用中channelのdisableはcause 1。
  channel TLVはcore §4.3で「分かる範囲で付ける」任意のため、付けた場合にぶつかったchannelを指すことだけを照合する。拒否は設定・generation・pinを変えない。
  count 0のset/unset、無いキーのunset、同じ内容に未知の非critical TLVを付けたsetはno-opでgenerationを保つ。
  複数の拒否条件の優先順位を新たに定めない。
- GENERATION：内容が変わるset/unsetごとに1進み、無いキーのunsetは進まない。
  明示制御`generation_at`で0xFFFFFFFEに置き、0xFFFFFFFFまで進めた後の内容変更はset/unsetともcause 2で断り、同じ内容の操作は成功する。再起動で1へ戻る。
- STORAGE-LIMIT：現在の設定が保存容量を超えてもsetは可能。saveをcause 3で拒否し、現在/保存を維持する。

generationは内容のdigestではなくboot内の変更カウンタとして、値そのもの（1、+1、上限）を照合する。
model内の保存の持ち方は検査oracleではない。操作の返り値、get/state、実際のGPIO/planを独立に照合する。
保存した項目の起動時の検証と部分適用は[保存復帰検査](config-fallback-conformance.ja.md)で確認する。

`uv run pytest -q tests/test_conformance_config.py`は36件。
正常1、共通宣言1、wire故障5、意味的故障24、制御不足4、channel TLV無しの受入れ1。
意味的故障は、erase時の現在設定消去、save欠落、setごとの自動保存、誤ったsaved_generation、同内容でのgeneration加算、起動時generation、bit7の保持、
使用中pinへのidle即時適用、空きpinへのidle遅延、disableの駆動、unset後の再設定漏れ、disable中channelの利用、
拒否時の設定/generation変更、使用中disableの受理、正規化した拒否tag、誤ったchannel TLV、重複キー受理、要求TLVの項目扱い、未知critical TLVの無視、
generationの折返し、無いキーのunsetでの加算、容量超過のcause違い、失敗saveの保存を含む。
旧config consumer/Endpointの自動attach・bind形は、この現仕様の適合根拠には使わない。
get paging/数値キー順は[別の部分検査](config-paging-conformance.ja.md)で確認する。
同内容saveの書き込み抑止と保存容量境界は[別の部分検査](config-storage-conformance.ja.md)で確認する。
channelごとのpull/出力の非対応、firmware自身のchannel、物理保存とproduction追従は残る。full_conformance=false。
