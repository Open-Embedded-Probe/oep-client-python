# oep-spec a09fa45 の節ごとの確認

oep-spec a09fa45 の core / transports / 各インターフェース文書の番号付きの節ごとに、その節を名指しして確かめる試験（tests/）と
独立した検査器（src/oep_client/conformance_*.py。その試験は tests/test_conformance_*.py）を挙げる。挙げたものはどれも全体の試験で通る。
数え方: 各ファイルの「<文書の名前> §<節>」の引用（「core §5.2/6.1」のような並びを含む）。

## core（docs/oep-core.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §0 | 範囲と層 | 2: `tests/test_capabilities.py`, `tests/test_request_tails.py` |
| §1 | 用語 | 40: `src/oep_client/conformance.py`, `src/oep_client/conformance_config_execution.py`, `src/oep_client/conformance_config_paging.py` |
| §1.1 | 規範の語 | 1: `src/oep_client/conformance_core_fixed_fields.py` |
| §1.2 | 適合 | 6: `src/oep_client/conformance.py`, `src/oep_client/conformance_registered_ops.py`, `tests/test_capabilities.py` |
| §2 | 共通の規則 | 57: `src/oep_client/conformance.py`, `src/oep_client/conformance_allocator.py`, `src/oep_client/conformance_attach.py` |
| §2.1 | byte order と文字列 | 3: `src/oep_client/conformance_discovery.py`, `tests/test_host_rules_2026_10_02.py`, `tests/test_virtual_bench_rules_2026_10_02.py` |
| §2.2 | TLV | 9: `src/oep_client/conformance.py`, `src/oep_client/conformance_host_core.py`, `src/oep_client/conformance_host_requests.py` |
| §2.3 | 固定の形と TLV | 34: `src/oep_client/conformance.py`, `src/oep_client/conformance_analog_payload.py`, `src/oep_client/conformance_arm_adi.py` |
| §2.4 | 知らない値 | 6: `src/oep_client/conformance.py`, `src/oep_client/conformance_host_core.py`, `src/oep_client/conformance_host_notifications.py` |
| §2.5 | 番号の空間 | 4: `tests/test_interfaces.py`, `tests/test_request_tails.py`, `tests/test_spec_sections.py` |
| §2.6 | 一周する値 | 6: `src/oep_client/conformance_capture_serial.py`, `src/oep_client/conformance_notification_retention.py`, `src/oep_client/conformance_stream.py` |
| §2.7 | 時計 | 1: `tests/test_spec_sections.py` |
| §2.8 | 名前と revision | 1: `tests/test_interfaces.py` |
| §3 | 経路 | 55: `src/oep_client/conformance.py`, `src/oep_client/conformance_analog_payload.py`, `src/oep_client/conformance_arm_adi.py` |
| §4 | メッセージ | 54: `src/oep_client/conformance.py`, `src/oep_client/conformance_analog.py`, `src/oep_client/conformance_arm_adi.py` |
| §4.1 | 要求 | 18: `src/oep_client/conformance.py`, `src/oep_client/conformance_corr.py`, `src/oep_client/conformance_exhausted.py` |
| §4.2 | 応答 | 3: `src/oep_client/conformance.py`, `src/oep_client/conformance_outcomes.py`, `src/oep_client/conformance_resources.py` |
| §4.3 | reject reason（本体） | 22: `src/oep_client/conformance.py`, `src/oep_client/conformance_capture_group.py`, `src/oep_client/conformance_config.py` |
| §4.4 | パイプライン | 28: `src/oep_client/conformance.py`, `src/oep_client/conformance_analog.py`, `src/oep_client/conformance_capture_group.py` |
| §5 | 立て直しと送り直し | 29: `src/oep_client/conformance.py`, `src/oep_client/conformance_arm_adi.py`, `src/oep_client/conformance_arm_adi_blocks.py` |
| §5.1 | 再送と結果の保持 | 10: `src/oep_client/conformance.py`, `src/oep_client/conformance_corr.py`, `src/oep_client/conformance_host_resend.py` |
| §6 | セッションと排他 | 29: `src/oep_client/conformance.py`, `src/oep_client/conformance_allocator.py`, `src/oep_client/conformance_analog.py` |
| §6.1 | ロック | 12: `src/oep_client/conformance.py`, `src/oep_client/conformance_config_execution.py`, `src/oep_client/conformance_corr.py` |
| §6.2 | 要求を受けたときの判定 | 8: `src/oep_client/conformance.py`, `src/oep_client/conformance_corr.py`, `src/oep_client/conformance_refusal_order.py` |
| §6.3 | ロックの要る要求 | 3: `src/oep_client/conformance.py`, `tests/test_conformance_reconnect.py`, `tests/test_virtual_bench_rules_2026_10_06.py` |
| §6.4 | open、end、keepalive、force | 11: `src/oep_client/conformance.py`, `src/oep_client/conformance_core_fixed_fields.py`, `src/oep_client/conformance_host_core.py` |
| §6.5 | boot_id | 3: `src/oep_client/conformance_retention_lifecycle.py`, `tests/test_host_rules_2026_10_06.py`, `tests/test_link_host.py` |
| §7 | 発見 | 13: `src/oep_client/conformance.py`, `src/oep_client/conformance_arm_adi.py`, `src/oep_client/conformance_capture_group.py` |
| §7.1 | confirm | 11: `src/oep_client/conformance.py`, `src/oep_client/conformance_config_paging.py`, `src/oep_client/conformance_discovery.py` |
| §7.2 | list | 11: `src/oep_client/conformance.py`, `src/oep_client/conformance_discovery.py`, `src/oep_client/conformance_interfaces.py` |
| §7.3 | describe | 5: `src/oep_client/conformance.py`, `src/oep_client/conformance_discovery.py`, `src/oep_client/conformance_stream.py` |
| §7.4 | describe の共通タグ（0x01〜0x3F） | 10: `src/oep_client/conformance.py`, `src/oep_client/conformance_blocks.py`, `src/oep_client/conformance_fixture_metadata.py` |
| §7.5 | probe 全体の宣言（fn 0 の describe、0x40〜） | 14: `src/oep_client/conformance.py`, `src/oep_client/conformance_config_execution.py`, `src/oep_client/conformance_config_paging.py` |
| §7.6 | clock | 1: `src/oep_client/conformance.py` |
| §8 | channel の空きの状態 | 11: `src/oep_client/conformance_analog.py`, `src/oep_client/conformance_capture_group.py`, `src/oep_client/conformance_capture_timeline_lifecycle.py` |
| §8.1 | 資源の取り合い | 3: `src/oep_client/conformance_scan.py`, `src/oep_client/conformance_scan_options.py`, `src/oep_client/conformance_wire_options.py` |
| §9 | 資源の寿命（一般の規則） | 43: `src/oep_client/conformance.py`, `src/oep_client/conformance_allocator.py`, `src/oep_client/conformance_analog.py` |
| §10 | 通知 | 4: `src/oep_client/conformance_capture_timeline.py`, `src/oep_client/conformance_capture_timeline_lifecycle.py`, `src/oep_client/conformance_subscriptions.py` |
| §10.1 | host の義務（全 host） | 1: `src/oep_client/conformance_host_notifications.py` |
| §10.2 | 形 | 6: `src/oep_client/conformance_capture_timeline.py`, `src/oep_client/conformance_host_notifications.py`, `src/oep_client/conformance_notification_sequence.py` |
| §10.3 | 購読 | 8: `src/oep_client/conformance_notification_retention.py`, `src/oep_client/conformance_notification_sequence.py`, `src/oep_client/conformance_subscriptions.py` |
| §10.4 | probe の義務（送り出す probe） | 2: `src/oep_client/conformance_notification_retention.py`, `src/oep_client/conformance_notification_sequence.py` |
| §11 | 本体（fn 0）の操作一覧 | 3: `src/oep_client/conformance_data.py`, `src/oep_client/conformance_host_core.py`, `tests/test_conformance_routes.py` |
| §12 | 拡張の規則（インターフェースの書き方） | 2: `tests/test_capabilities.py`, `tests/test_spec_sections.py` |
| §12.1 | 伸び方 | 1: `tests/test_spec_sections.py` |
| §13 | 規範ではない文書（ガイド） | ガイドの案内（規範ではない） |

## transports（docs/oep-transports.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §1 | フレーム | 4: `src/oep_client/conformance.py`, `tests/test_link_serial.py`, `tests/test_spec_vectors.py` |
| §2 | フレームの送り方 | 2: `src/oep_client/conformance.py`, `src/oep_client/conformance_tcp_recovery.py` |
| §3 | 複数の経路 | 13: `src/oep_client/conformance.py`, `src/oep_client/conformance_host_deadline.py`, `src/oep_client/conformance_link_routes.py` |
| §4 | シリアルの口 | 3: `tests/test_link_serial.py`, `tests/test_port_speed.py`, `tests/test_virtual_bench_spec.py` |
| §5 | 区切りの立て直し | 3: `src/oep_client/conformance_host_resend.py`, `src/oep_client/conformance_tcp_recovery.py`, `tests/test_host_rules_2026_10_02.py` |
| §6 | host の待ちの転送の時間 | 3: `src/oep_client/conformance_host_deadline.py`, `src/oep_client/conformance_host_resend.py`, `src/oep_client/conformance_host_wait.py` |
| §7 | 参照する仕様 | 参照規格の一覧（振る舞いを定めない） |

## common（interfaces/oep-if-common.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §1 | 位置つきのストリーム | 9: `src/oep_client/conformance_cut.py`, `src/oep_client/conformance_dependencies.py`, `src/oep_client/conformance_reset.py` |
| §1.1 | 位置 | 2: `src/oep_client/conformance_stream.py`, `tests/test_interfaces.py` |
| §1.2 | read | 1: `tests/test_virtual_bench_rules_2026_10_02.py` |
| §1.3 | マーク | 4: `src/oep_client/conformance_capture_serial.py`, `tests/test_conformance_stream.py`, `tests/test_spec_2c6d18d.py` |
| §1.4 | 状態を変える操作 | 2: `src/oep_client/conformance_mailbox.py`, `src/oep_client/conformance_outcomes.py` |
| §1.5 | 通知のデータ | 1: `tests/test_virtual_bench_capture.py` |
| §2 | debug の connection | 8: `src/oep_client/conformance_blocks.py`, `src/oep_client/conformance_cut.py`, `src/oep_client/conformance_dependencies.py` |
| §3 | 線と target の操作の status | 6: `src/oep_client/conformance_blocks.py`, `src/oep_client/conformance_capture_serial.py`, `src/oep_client/conformance_outcomes.py` |

## plan（interfaces/oep-if-plan.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §1 | 操作 | 4: `src/oep_client/conformance_registered_ops.py`, `tests/test_plan.py`, `tests/test_request_tails.py` |
| §2 | plan | 8: `src/oep_client/conformance_analog.py`, `src/oep_client/conformance_capture_group.py`, `src/oep_client/conformance_gpio.py` |
| §2.1 | plan_apply | 3: `tests/test_plan.py`, `tests/test_request_tails.py`, `tests/test_spec_vectors.py` |
| §2.2 | plan_release | 1: `tests/test_plan.py` |
| §2.3 | ピン | 1: `tests/test_plan.py` |
| §2.4 | 断り方 | 1: `tests/test_plan.py` |

## restart（interfaces/oep-if-restart.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §1 | 操作 | 6: `src/oep_client/conformance_registered_ops.py`, `src/oep_client/conformance_restart.py`, `src/oep_client/conformance_selected_metadata.py` |
| §2 | restart | 2: `src/oep_client/conformance_restart.py`, `tests/test_restart.py` |

## link（interfaces/oep-if-link.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §1 | 操作 | 4: `src/oep_client/conformance_link_transfers.py`, `src/oep_client/conformance_registered_ops.py`, `tests/test_port_speed.py` |
| §2 | 線の試験（source、sink） | 4: `src/oep_client/conformance_link_routes.py`, `tests/test_console_queue_and_reset_settle.py`, `tests/test_linktest.py` |
| §3 | port_speed | 7: `src/oep_client/conformance_port_speed.py`, `src/oep_client/conformance_port_speed_execution.py`, `src/oep_client/conformance_port_speed_lifecycle.py` |

## fixture（interfaces/oep-if-fixture.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §1 | `oep.fixture.gpio` | 8: `src/oep_client/conformance_fixture_metadata.py`, `src/oep_client/conformance_gpio.py`, `src/oep_client/conformance_registered_ops.py` |
| §2 | `oep.fixture.uart` | 10: `src/oep_client/conformance_oep_smoke.py`, `src/oep_client/conformance_registered_ops.py`, `src/oep_client/conformance_uart.py` |
| §3 | `oep.fixture.i2c-target` | 9: `src/oep_client/conformance_i2c.py`, `src/oep_client/conformance_registered_ops.py`, `tests/test_capabilities.py` |
| §4 | `oep.fixture.spi-target` | 6: `src/oep_client/conformance_registered_ops.py`, `src/oep_client/conformance_spi.py`, `tests/test_fixture_targets.py` |
| §5 | 参照する仕様 | 参照規格の一覧 |

## debug（interfaces/oep-if-debug.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §0 | 本書の wire が共有する規則、別の wire が定めるもの | 2: `src/oep_client/conformance_oep_declarations.py`, `src/oep_client/conformance_scan.py` |
| §1 | attach の規範（本書の3つのwire） | 19: `src/oep_client/conformance_attach.py`, `src/oep_client/conformance_debug.py`, `src/oep_client/conformance_host_requests.py` |
| §2 | connection の寿命（本書の3つのwire） | 16: `src/oep_client/conformance_arm_adi.py`, `src/oep_client/conformance_arm_adi_blocks.py`, `src/oep_client/conformance_cut.py` |
| §2.1 | connections（接続の一覧） | 1: `src/oep_client/conformance_wire_options.py` |
| §3 | `oep.wire.rvswd` / `oep.wire.swio` | 16: `src/oep_client/conformance_attach.py`, `src/oep_client/conformance_host_requests.py`, `src/oep_client/conformance_host_values.py` |
| §3.1 | RVSWD のフレーム | probe の線のビット単位の波形（物理層）。仮想ベンチはビットを模さず、このクライアントの範囲外 |
| §3.2 | SWIO のフレーム | 1: `src/oep_client/conformance_oep_declarations.py` |
| §4 | `oep.target.riscv-dm` | 14: `src/oep_client/conformance_blocks.py`, `src/oep_client/conformance_debug.py`, `src/oep_client/conformance_host_values.py` |
| §4.1 | dmi | 2: `src/oep_client/conformance_debug.py`, `src/oep_client/conformance_retry.py` |
| §4.2 | halt、resume、step | 5: `src/oep_client/conformance_debug.py`, `src/oep_client/conformance_step.py`, `tests/test_host_rules_2026_10_02.py` |
| §4.3 | reset | 7: `src/oep_client/conformance_reset.py`, `tests/test_console_queue_and_reset_settle.py`, `tests/test_host_rules_2026_10_02.py` |
| §4.4 | run | 4: `src/oep_client/conformance_run.py`, `tests/test_interfaces.py`, `tests/test_spec_2c6d18d.py` |
| §4.5 | read_block、write_block | 4: `src/oep_client/conformance_blocks.py`, `src/oep_client/conformance_selected_metadata.py`, `tests/test_target_parts.py` |
| §4.6 | RISC-V の connection の扱い | 2: `src/oep_client/conformance_dmseq.py`, `src/oep_client/conformance_reset.py` |
| §5 | `oep.wire.swd` | 11: `src/oep_client/conformance_attach.py`, `src/oep_client/conformance_blocks.py`, `src/oep_client/conformance_host_requests.py` |
| §6 | `oep.target.arm-adi` | 8: `src/oep_client/conformance_arm_adi.py`, `src/oep_client/conformance_arm_adi_blocks.py`, `src/oep_client/conformance_dmseq.py` |
| §7 | 参照する仕様 | 参照規格の一覧 |

## console（interfaces/oep-if-console.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §1 | 操作 | 9: `src/oep_client/conformance_attach.py`, `src/oep_client/conformance_dmseq.py`, `src/oep_client/conformance_mailbox.py` |
| §2 | ストリームの規則 | 8: `src/oep_client/conformance_attach.py`, `src/oep_client/conformance_dmseq.py`, `src/oep_client/conformance_mailbox.py` |
| §3 | 方式（mechanism） | 10: `src/oep_client/conformance_attach.py`, `src/oep_client/conformance_debug.py`, `src/oep_client/conformance_dmseq.py` |
| §3.1 | SDI | 3: `src/oep_client/conformance_mailbox.py`, `tests/test_console_queue_and_reset_settle.py`, `tests/test_interfaces.py` |
| §3.2 | DMDATA | 1: `src/oep_client/conformance_mailbox.py` |
| §4 | 参照する仕様 | 参照規格の一覧 |

## probe-config（interfaces/oep-if-probe-config.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §1 | 項目 | 16: `src/oep_client/conformance_config.py`, `src/oep_client/conformance_config_fallback.py`, `src/oep_client/conformance_host_request_sample.py` |
| §2 | 操作と変更の世代 | 14: `src/oep_client/conformance_config.py`, `src/oep_client/conformance_config_execution.py`, `src/oep_client/conformance_config_fallback.py` |
| §3 | 保存と起動 | 9: `src/oep_client/conformance_config.py`, `src/oep_client/conformance_config_fallback.py`, `src/oep_client/conformance_config_power_loss.py` |
| §4 | describe | 6: `src/oep_client/conformance_capture_metadata.py`, `src/oep_client/conformance_config.py`, `src/oep_client/conformance_config_storage.py` |

## capture（interfaces/oep-if-capture.ja.md）

| 節 | 見出し | 名指す試験・検査器（件数、先頭3つ） |
|---|---|---|
| §1 | データの形 | 12: `src/oep_client/conformance_capture_group.py`, `src/oep_client/conformance_capture_group_geometry.py`, `src/oep_client/conformance_capture_timeline.py` |
| §1.1 | ロジック | 2: `tests/test_spec_2c6d18d.py`, `tests/test_spec_vectors.py` |
| §1.2 | アナログ | 3: `src/oep_client/conformance_analog.py`, `src/oep_client/conformance_analog_payload.py`, `tests/test_virtual_bench_capture.py` |
| §2 | モードと区画 | 11: `src/oep_client/conformance_analog.py`, `src/oep_client/conformance_analog_payload.py`, `src/oep_client/conformance_capture_group_geometry.py` |
| §2.1 | モード | 4: `src/oep_client/conformance_capture_timeline.py`, `src/oep_client/conformance_capture_timeline_lifecycle.py`, `tests/test_spec_vectors.py` |
| §2.2 | 区画 | 3: `src/oep_client/conformance_capture_serial.py`, `src/oep_client/conformance_capture_timeline.py`, `tests/test_spec_2c6d18d.py` |
| §3 | 操作 | 11: `src/oep_client/conformance_analog.py`, `src/oep_client/conformance_analog_payload.py`, `src/oep_client/conformance_capture_group_geometry.py` |
| §3.1 | 役割（plan） | 1: `tests/test_spec_sections.py` |
| §3.2 | 操作 | 7: `src/oep_client/conformance_analog_payload.py`, `src/oep_client/conformance_capture_serial.py`, `src/oep_client/conformance_capture_timeline.py` |
| §3.3 | configure | 8: `src/oep_client/conformance_analog_payload.py`, `src/oep_client/conformance_capture_group_geometry.py`, `src/oep_client/conformance_host_requests.py` |
| §3.4 | 通知（core §10） | 7: `src/oep_client/conformance_capture_timeline.py`, `src/oep_client/conformance_capture_timeline_lifecycle.py`, `src/oep_client/conformance_registered_ops.py` |
| §3.5 | describe で宣言するもの | 2: `src/oep_client/conformance_capture_metadata.py`, `tests/test_conformance_capture_metadata.py` |
| §3.6 | 拡張の境界 | 1: `tests/test_spec_sections.py` |
| §3.7 | calibration（アナログ） | 3: `src/oep_client/conformance_analog.py`, `src/oep_client/conformance_analog_payload.py`, `src/oep_client/conformance_registered_ops.py` |
| §4 | `oep.fixture.capture-group`（即時の一括開始） | 10: `src/oep_client/conformance_capture_group.py`, `src/oep_client/conformance_capture_timeline.py`, `src/oep_client/conformance_capture_timeline_lifecycle.py` |
| §4.1 | 操作 | 9: `src/oep_client/conformance_capture_group.py`, `src/oep_client/conformance_capture_group_geometry.py`, `src/oep_client/conformance_capture_timeline.py` |
| §4.2 | 通知 | 1: `tests/test_spec_vectors.py` |
| §4.3 | describe | 2: `src/oep_client/conformance_capture_metadata.py`, `src/oep_client/conformance_registered_ops.py` |

