# 段階 9 / 9b の規則ごとの確認（oep-spec a09fa45）

oep.probe.config と oep.target.console の規範の規則を 1 つずつ、それを確かめる検査（独立した検査器の case、または
仕様のベクトルを当てる試験）と、その規則に違反するモデルで検査が失敗することを示す故障注入（mutant）に対応させる。
段階 9 は本体（クライアントと仮想ベンチ）、段階 9b は独立した検査器である。どれも全体の試験で通る。

## probe.config（interfaces/oep-if-probe-config.ja.md）

| 規則 | 検査 | 故障注入 |
|---|---|---|
| §1 項目は tag len value、host は bit7 を 0 で送り probe は無視する | ConfigChecks PERSISTENCE / ATOMIC、ConfigPagingChecks | `bit7_kept`、`normalized_tag`、paging `critical_bit` |
| §1 項目は外側の TLV と別の容器。知らない項目は受け取った tag で unsupported | ATOMIC（0xFF、旧 0x01） | `normalized_tag`、`tail_as_item` |
| §1 外側の未知の非 critical TLV は無視、未知の critical は副作用前に断る | ATOMIC | `tail_as_item`、`ignore_critical_tail` |
| §1 項目番号 0 は malformed | ATOMIC（tag 128） | `accept_item_zero` |
| §1 channel が channels 以上か firmware のものなら unsupported | ATOMIC | `accept_channel_past` |
| §1 知っている項目の長さ違いは malformed | ATOMIC | `accept_wrong_length` |
| §1 disable は起動時・解放時の空き状態より先に効き、駆動しない | IDLE、ConfigFallbackChecks CONFLICT | `disable_drives`、fallback `idle_wins` |
| §1 無効な channel を使う要求は cause 5 | IDLE | `disabled_usable` |
| §1 使用中の channel の disable は cause 1、idle と disable の併存は cause 5、何も変えない | ATOMIC | `accept_disabled_live`、`refusal_changes_config` |
| §1 disable の unset はただちに空き状態へ | IDLE | `unset_keeps_disable` |
| §1 idle は空きならすぐ、使用中なら次に空いたときに効く | IDLE | `idle_waits_free`、`idle_changes_live_pin` |
| §1 対応しない mode は unsupported | ATOMIC（mode 5） | `accept_unknown_mode` |
| §2 容量を超える変更は cause 2 / 3 | STORAGE-LIMIT、ConfigStorageChecks LIMIT | `wrong_storage_cause`、storage `capacity63` / `capacity65` / `wrong_cause` |
| §2 set はキーごとに置換、unset は無いキーで no-op、重複キーは malformed | ATOMIC / GENERATION | `accept_duplicate`、`unset_missing_counts` |
| §2 変更後の全体を先に検証し、拒否では設定・世代・ピンを保つ | ATOMIC | `refusal_changes_config`、`refusal_counts` |
| §2 設定を変えない set / unset はピンを再設定しない | ATOMIC | `noop_touches_pins` |
| §2 generation は起動時 1、変更ごとに +1、同じ内容は不変、0xFFFFFFFF の変更は cause 2 | GENERATION / PERSISTENCE | `boot_generation`、`count_same_contents`、`wrap_generation` |
| §2 get は tag・channel 順、first からページ、同じ世代は同じ内容、TLV を項目に混ぜない | ConfigPagingChecks | paging の 19 個（`byte_key_order`、`split_item`、`tail_counted_as_item` ほか） |
| §3 save は全項目、同じ内容なら書き直さない、電源断で旧か新の完全な保存 | ConfigStorageChecks、ConfigPowerLossChecks | storage `always_write`、power-loss の 19 個 |
| §3 save / erase は成否とも現在の設定・世代・ピンを変えない | PERSISTENCE、STORAGE-LIMIT | `erase_current`、`failed_save_changes_saved`、storage `save_changes_pin` |
| §3 起動時の項目ごとの検証、disable 優先、重複キー不適用、容量の選び方、壊れた容器 | ConfigFallbackChecks | fallback の 20 個 |
| §3 結果の表（失敗後の state） | STORAGE-LIMIT、interface_boundaries.json storage | `failed_save_changes_saved`、ベクトル 9 件 |
| §3 save の実行は max_op_ms 内、lease・writer の扱い | ConfigExecutionChecks | execution の 15 個 |
| §4 storage は save / erase を持つときだけ必須、items は必須で 1 つ以上・重複なし | SelectedMetadataChecks、CaptureMetadataChecks の config 部分 | metadata `(17, [])` ほか |

## oep.target.console（interfaces/oep-if-console.ja.md）

| 規則 | 検査 | 故障注入 |
|---|---|---|
| §1 read / marks / clear / mark / write は共通部品 §1 の形 | StreamChecks、DependencyChecks | stream の 12 個、marks の 8 個 |
| §1 describe mechanisms は n(u8) n × mechanism、n ≥ 1、重複なし、必須 | OepDeclarationChecks MECHANISMS、AttachChecks | `missing_mechanisms`、`uncounted_mechanisms`、`duplicate_mechanism`、`empty_mechanisms`、`unknown_mechanism`、`preset_none` |
| §1 宣言に無い mechanism は unsupported（0x00） | AttachChecks CONSOLE-UNSUPPORTED、ベクトル | `arm_console` |
| §1 知らない stream は no_resource、別の種類の番号は cause 6 | DependencyChecks、CutChecks | `marks_wrong_resource`、cut `stream_kept` |
| §1 stream の番号は u32 で同じ boot の間は再利用しない | DependencyChecks、AttachChecks | `reuse_id`、`console_extra_byte` |
| §1 streams は生きている stream を作成順に返す | AttachChecks CONSOLE-INDEPENDENT | `streams_reversed` |
| §1 同じ connection の 2 つ目の open は cause 6 | AttachChecks CONSOLE-DUPLICATE | `duplicate_stream` |
| §1 marks の無い fn の read from 3 は unsupported、SDI への write は unsupported | ops.json「console read: from3 without marks」「console write: SDI」、MailboxChecks SDI-WRITE | `sdi_write_taken` |
| §2 close・connection の解放・セッションの終わりで stream とバッファ・送信列を解放 | DependencyChecks、WriterLeaseChecks | `orphan`、`close_parent`、`retain_end`、lease `resources_left` |
| §2 clear は読みのバッファだけ、送りの列に触れない | DependencyChecks | `clear_tx` |
| §2 riscv-dm の要求の実行中と hart が止まっている間はポーリングしない | MailboxChecks、DmseqChecks | `halt_access`、`busy_access`、dmseq `halt_access` / `debug_access` |
| §2 reset マーク 0x40 は reset op の成功時だけ、restart 0x41 は havereset | ResetChecks CONSOLE / CONSOLE-DM-LINE、DmseqChecks | `mark_detail`、`missing_mark`、`mark_on_failure`、dmseq の mark 5 個 |
| §2 console の読みで線切れを判断したら、マークなしで connection と stream を閉じる | CutChecks CONSOLE | `stale_mark`、`stream_kept`、`single_line` |
| §3.1 SDI: L 1〜7 で DATA1 を読み DATA0 を 0、L ≥ 8 は DATA0 を 0、host から target の向きは無い | MailboxChecks SDI-* | `sdi_skip_data1`、`ack_not_written`、`sdi_clear_invalid`、`sdi_write_taken` |
| §3.2 DMDATA の枠の順番と長さ、自分の枠を書き換えない | MailboxChecks DM-* | `dm_*` の 7 個 |
| §3.3 dmseq の枠・CRC・重複排除・resync | DmseqChecks、checks.json の CRC-8 と DATA0 | dmseq の 15 個 |

この照合で、本体と検査器のモデルが mechanisms を count なしで宣言していたこと（§1 の n(u8) を欠く）を見つけ、直した。
