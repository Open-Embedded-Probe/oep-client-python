# Changelog / 変更履歴

## Unreleased
- (EN) Breaking: the wire follows oep-spec's zero-base rewrite of 2026-10-01 (docs/v1-zero-base-proposal.ja.md §3 / §7; registry synced). Every answer that ended in data or a list now carries its length and may be followed by TLVs: read answers `start flags len data [TLV]` (console, fixture UART; capture `len` u32), gpio read `n n×level`, dmi `done status nvals values`, arm-adi transfer `done status ack nvals values`, run `status stopped dpc elapsed_us nvals values` (stopped 2 = the hart could not be halted: `RunResult.not_halted`), data frames `position len data [TLV]`, events `kind fixed-part [TLV]`. TLVs have a long form (`tag 0xFF len(u16) value` for 255 bytes and more; `message.tlv` / `split_tlvs`, the long form with a short value is `message.BadTlv`). confirm answers the probe's `boot_id`; subscribe's `max_delay_ms` is u32; `rejected unsupported` is always `tag [TLV]` (0x00 = a fixed-part value: `Unsupported.tag` None, `.tlvs`).
- (JA) 破壊的変更: wire を oep-spec の 2026-10-01 のゼロベース見直し（docs/v1-zero-base-proposal.ja.md §3 / §7。registry を写し直した）に揃えた。データや並びで終わっていた応答はすべて長さを持ち、後ろに TLV を置ける: read の応答 `start flags len data [TLV]`（console、fixture UART。capture の `len` は u32）、gpio read `n n×level`、dmi `done status nvals values`、arm-adi transfer `done status ack nvals values`、run `status stopped dpc elapsed_us nvals values`（stopped 2 = hart を止められなかった: `RunResult.not_halted`）、データフレーム `position len data [TLV]`、出来事 `kind 固定部 [TLV]`。TLV に長い形（255 byte 以上は `tag 0xFF len(u16) value`。`message.tlv` / `split_tlvs`、短い値の長い形は `message.BadTlv`）。confirm は probe の `boot_id` を返す。subscribe の `max_delay_ms` は u32。`rejected unsupported` は常に `tag [TLV]`（0x00 = 固定部の値: `Unsupported.tag` は None、`.tlvs`）。
- (EN) Breaking: after the lease lapsed, the same session id's next request is `rejected expired` (0x0E): `host.Expired(Rejected)` with `.lease_ms`, never re-opened silently; `open()` then answers `Opened.resumed == 2` (`.swept`) and `Host.epoch` moves (as it does on `no_session` - what an id forced out by another meets after `locked`). `Wire.connections()` and `Console.streams()` page through `first` / `more`. Subscriptions survive a same-id open while the lock is held; a subscribe to an fn that emits nothing is unsupported, an unsubscribe of nothing is ok.
- (JA) 破壊的変更: lease が切れた後の同じ session_id の要求は `rejected expired`（0x0E）: `host.Expired(Rejected)`（`.lease_ms` つき）。黙って open し直さない。その後の `open()` は `Opened.resumed == 2`（`.swept`）を返し、`Host.epoch` が進む（`no_session` でも進む。force で奪われた側は locked のあと no_session）。`Wire.connections()` と `Console.streams()` は `first` / `more` でページを辿る。保持中の同じ ID の open で購読は残る。送り出さない fn の subscribe は unsupported、購読の無い unsubscribe は ok。
- (EN) Breaking: `oep.probe.config` - describe is declarations only (`ProbeConfig.describe()` -> `Declared`: storage `max_bytes` u32, items, slots_max, bind_modes u32); the live storage / slot / bind state is the lock-free `state` op (0x06, paged; `ProbeConfig.state()` -> `State`, `SlotState.last_try_at_ns`); `unset` (0x05) removes items by key (`ProbeConfig.unset`, `config.remove()` in a `set` list goes as an unset - a key alone no longer removes); the new `uart` item (`config.Uart(fn, baud, format)`) is applied whenever that UART's plan has RX or TX (a session's configure wins until the plan goes); slot fields are `retry_ms` (u32; the Python `Slot.retry_s` stays, seconds) and `max_speed_hz` (u32; `Slot.max_speed` stays, Hz); mechanism `none` (0xFF) = a slot without a console; the hash is the CRC-32 of the canonical form (`config.canonical` / `hash_of`). CLI: `oep config state`, `oep config uart`, `remove` for every item kind.
- (JA) 破壊的変更: `oep.probe.config` - describe は宣言だけ（`ProbeConfig.describe()` -> `Declared`: storage は `max_bytes` u32、items、slots_max、bind_modes u32）。保存・スロット・bind の今の状態はロック不要の `state` op（0x06、ページング。`ProbeConfig.state()` -> `State`、`SlotState.last_try_at_ns`）。`unset`（0x05）はキーで項目を消す（`ProbeConfig.unset`。`set` の並びの `config.remove()` は unset として送る。キーだけの項目で消す規則は廃止）。新しい `uart` 項目（`config.Uart(fn, baud, format)`）は、その UART の plan に RX / TX が付くたびに掛かる（セッションの configure は plan を解くまで勝つ）。スロットのフィールドは `retry_ms`（u32。Python の `Slot.retry_s` は秒のまま）と `max_speed_hz`（u32。`Slot.max_speed` は Hz のまま）。mechanism `none`（0xFF）= コンソール無しのスロット。hash は正規形の CRC-32（`config.canonical` / `hash_of`）。CLI: `oep config state`、`oep config uart`、`remove` はすべての項目。
- (EN) Breaking: wires - `attach_under_reset` (op 0x04) is gone: `Wire.attach(reset=(channel, hold_ms))` sends the attach's critical TLV 0x05 (the `attach_under_reset()` method stays as a wrapper and returns the dpc from the answer's TLV 0x11); `max_speed` is required by the probe, so `attach()` always sends one (None: the wire's declared `max_clock_hz`, else 1 MHz); the answer's flags are the common `attach_flags` (`Wire.halted` / `.dpc`; swd's dormant bit moved to bit2); scan takes `max_speed` / `idle_clock` TLVs and its skip is TLV 0x02; `WireBase.connections()` and `detach(force=)`. `RiscvDm.run(timeout_ms=None)` is the probe's `max_op_ms` (`core.max_op_ms`), no longer 0xFFFFFFFF.
- (JA) 破壊的変更: 線 - `attach_under_reset`（op 0x04）は無くなり、`Wire.attach(reset=(channel, hold_ms))` が attach の critical TLV 0x05 を送る（`attach_under_reset()` メソッドは包みとして残り、応答の TLV 0x11 の dpc を返す）。probe は `max_speed` を必須にしたので `attach()` は常に送る（None: その線の宣言の `max_clock_hz`、無ければ 1 MHz）。応答の flags は共通の `attach_flags`（`Wire.halted` / `.dpc`。swd の dormant は bit2 に移った）。scan は `max_speed` / `idle_clock` の TLV を取り、skip は TLV 0x02。`WireBase.connections()` と `detach(force=)`。`RiscvDm.run(timeout_ms=None)` は probe の `max_op_ms`（`core.max_op_ms`）で、0xFFFFFFFF ではなくなった。
- (EN) Breaking: console - marks carry `time_ns` (u64, the probe's one clock; `Mark.time_ns`, 22 bytes), a stream lives while anything uses it (the sessions that opened it, a bound slot; `close` takes one share; a lapse takes the session's), one live stream per connection (another mechanism: unavailable cause 6), a closed stream of the same place and mechanism comes back under its old number, mark `closed` (0x09) with why, the lock-free `streams` op (`Console.streams()` -> `StreamInfo`), a write that fits nothing is completed failed. Connections and streams share one u16 number space (wrapping); a number of the other kind is unavailable cause 6. `registry.COMMON.enum` holds `read_from`, `read_flags`, `mark_kind` and the mark details (they left the console's enum).
- (JA) 破壊的変更: console - マークは `time_ns`（u64、probe の 1 本の時計。`Mark.time_ns`、22 byte）。ストリームは使っているもの（open したセッション、bind されたスロット）がある間生きる（`close` は自分の分、lease 切れはセッションの分を外す）。1 つの connection に生きているストリームは 1 つ（別の mechanism は unavailable cause 6）。閉じたストリームは同じ場所・同じ mechanism の open で同じ番号で戻る。マーク `closed`（0x09）に理由。ロック不要の `streams` op（`Console.streams()` -> `StreamInfo`）。何も入らなかった write は completed failed。connection とストリームは 1 つの u16 の番号空間（一周する）で、別の種類の番号は unavailable cause 6。`registry.COMMON.enum` に `read_from`、`read_flags`、`mark_kind` とマークの detail（console の enum から移った）。
- (EN) Breaking: capture - every start is a generation (u32): start answers `blocking_ms generation`, status `state serial_done write_pos flags generation [TLV error]` (`LogicCapture.status()` -> `Status`, still iterable as the old 4-tuple), segment records are 37 bytes (`Segment.generation`), read is `generation position max`, release `generation serial` (another generation: unavailable cause 6), streaming data frames carry TLV generation (`stream()` drops another generation's, `Received.stale`); `LogicCapture.generation` / `read(..., generation=)`; `read_segment(segment)` passes the segment's own. Trigger values are u32; segments answers have `more`; state 5 goes on by itself after release (no stopped 2 event); describe `channels` layouts / `trigger` types are u32, `rate_list` / group `tracks` / `budget` carry `n`; `Calibration.vrefint_nominal_mv`, factory data has `raw_len`; the group's start answers each track's generation (`CaptureGroup.generations`), its events are triggered 0x03 / stopped 0x02.
- (JA) 破壊的変更: capture - start ごとに世代（u32）: start の応答 `blocking_ms generation`、status `state serial_done write_pos flags generation [TLV error]`（`LogicCapture.status()` -> `Status`。前の 4 つ組としても展開できる）、区画の情報は 37 byte（`Segment.generation`）、read は `generation position max`、release は `generation serial`（違う世代は unavailable cause 6）、ストリーミングのデータフレームは TLV generation を持つ（`stream()` は別の世代を捨てる。`Received.stale`）。`LogicCapture.generation` / `read(..., generation=)`。`read_segment(segment)` は区画の世代を付ける。trigger の value は u32。segments の応答に `more`。state 5 は release で自動で再開（stopped 2 の出来事は無い）。describe の `channels` の layout / `trigger` の types は u32、`rate_list` / 組の `tracks` / `budget` は `n` を持つ。`Calibration.vrefint_nominal_mv`。factory に `raw_len`。組の start は各トラックの世代を返す（`CaptureGroup.generations`）、組の出来事は triggered 0x03 / stopped 0x02。
- (EN) Breaking: fixture - gpio read answers `n n×level`, its describe `modes` is u32 (a mode the probe lacks is unsupported with channel / index TLVs; mode 8+ malformed); the UART's stream is made by the plan (RX or TX) and goes with it, its position never going back within a boot; `FixtureUart.status()` (op 0x07) -> `UartStatus`; an undefined format bit is malformed, an undeclared format unsupported, a baud more than 5 % off unsupported; i2c / spi `errors` are u32, `read_rx` reads its TLV tail (`last_ns`), describe `queue_depth`.
- (JA) 破壊的変更: fixture - gpio read の応答は `n n×level`、describe の `modes` は u32（持たない mode は channel / index の TLV つきの unsupported。mode 8 以上は malformed）。UART のストリームは plan（RX か TX）が作り、plan と一緒に消える。位置は起動の中で戻らない。`FixtureUart.status()`（op 0x07）-> `UartStatus`。format の未定義のビットは malformed、宣言に無い format は unsupported、±5% を超える baud は unsupported。i2c / spi の `errors` は u32、`read_rx` は TLV の後ろを読む（`last_ns`）、describe に `queue_depth`。
- (EN) The fake probe follows all of it (the working spec): `boot_id` in confirm, `expired` and `resumed` 2, one resource number space, the heartbeat `boot_id uptime_ns`, the plan's `plan_roles` limit and reason table, attach's reset TLV / required max_speed / scan TLVs, dmi / run answers with nvals (run's `FakeTarget.unstoppable` for stopped 2), the console's stream lifetime and `streams`, gpio modes, the UART's plan-made stream / `status` / formats, probe.config's `state` / `unset` / `uart` item / canonical hash, capture generations and the group's generations TLV. Its describes declare `max_op_ms` 10000, `plan_roles`, `discoverable` (was `oep_pid`), gpio `modes`, UART `formats`. USB: the vendor interface is told by class 0xFF, subclass 0x4F, protocol 0x45 and a probe by its iProduct `OEP` alone (`registry.USB`).
- (JA) fake の probe もすべてに追随した（動く spec）: confirm の `boot_id`、`expired` と `resumed` 2、資源番号の 1 つの空間、ハートビート `boot_id uptime_ns`、plan の `plan_roles` の上限と断り方の表、attach の reset TLV / 必須の max_speed / scan の TLV、nvals を持つ dmi / run の応答（stopped 2 は `FakeTarget.unstoppable`）、console のストリームの寿命と `streams`、gpio の modes、UART の plan が作るストリーム / `status` / formats、probe.config の `state` / `unset` / `uart` 項目 / 正規形の hash、capture の世代と組の世代の TLV。describe は `max_op_ms` 10000、`plan_roles`、`discoverable`（`oep_pid` から改名）、gpio の `modes`、UART の `formats` を宣言する。USB: vendor の interface は class 0xFF・subclass 0x4F・protocol 0x45 で、probe は iProduct `OEP` だけで見分ける（`registry.USB`）。

## 0.0.21
- (EN) Breaking: a bind's streams carry a length each (oep-spec probe.config §1.2, freeze decision 1, missed until now): `n × (len, kind, id)`, len 3 today; the fake skips a longer one's tail and refuses one under 3 as malformed.
- (JA) 破壊的変更: bind のストリームの並びの各要素の前に長さを置く（oep-spec probe.config §1.2。凍結の決定 1 で漏れていた）: `n × (len、kind、id)`、今の len は 3。fake は長い要素の後ろを飛ばし、3 未満は malformed で断る。
- (EN) `usb:X` is always a unit id, whatever its length (a 4-character unit id was read as a VID); a VID comes with its PID, `usb:VID:PID[:SERIAL]`.
- (JA) `usb:X` は長さによらず unit id として読む（4 文字の unit id を VID と読んでいた）。VID は PID と一緒に `usb:VID:PID[:SERIAL]` で書く。
- (EN) Breaking: the CLI's `name#k` is the list's instance, from 0 (oep-spec core §7.2's `name#instance`; `#0` may be left out). It was the k-th from 1.
- (JA) 破壊的変更: CLI の `name#k` は list の instance（0 から）にした（oep-spec core §7.2 の `name#instance`、`#0` は省ける）。1 から数えた k 番目だった。
- (EN) The fake probes number each interface's instance from 0 per name (core §7.2), as the probe firmware now does; they had arbitrary numbers.
- (JA) fake の probe はインターフェースの instance を名前ごとに 0 から振る（core §7.2）。probe の firmware も同じにした。これまでは適当な番号だった。
- (EN) README: `registry.INTERFACES[name]` is the public way to reach an interface's numbers by name.
- (JA) README: 名前からインターフェースの番号を引く公開の入口は `registry.INTERFACES[name]` と書いた。

## 0.0.20
- (EN) A console write's chunk fits the probe's frame: frame - 12 - the stream's prefix (the console's stream u16; none on a fixture UART). It was frame - 13 for both, one byte over on a console with 64-byte frames. Found porting it to oep-client-js.
- (JA) console の書き込みの 1 回の大きさが probe のフレームに収まるようにした: frame − 12 − stream の前置き（console は stream の u16、fixture UART は無し）。両方とも frame − 13 だったので、64 byte のフレームの console で 1 byte はみ出していた。oep-client-js に移すときに見つかった。

## 0.0.19
- (EN) The pre-freeze decisions (oep-spec docs/v1-freeze-decisions.ja.md), in the fake and the client at once:
  - `oep.fixture.capture` is `oep.fixture.logic`; the I2C / SPI targets are the standard `oep.fixture.i2c-target` / `spi-target` (`fixture.I2cTarget` / `SpiTarget`; `esp32_targets` is gone). Their status is separate fields (`I2cStatus(state=, mode=, armed=, queued=, …)`); `read_hw` is gone, `set_stretch` is `stretch` (op 0x07).
  - Answer lists put each element's length first (core §2.3): list, scan, connections, marks, capture segments (`message.Reader.element()`, `message.element()`); what an element carries after the known fields is skipped.
  - probe.config: a slot's lock is length-prefixed (`lock_len`, 0 = none); settings are saved with the (name, instance, revision) of each interface they name and renumbered at boot, so another interface added or moved leaves them in force (the fake: `saved_ids`); the storage says why it is unreadable (`State.unreadable`). The item classes (`Plan`, `Label`, `Idle`, `Slot`, `Bind`) take keyword arguments only.
  - rejected unavailable carries core §4.3's TLVs: `host.Unavailable` with `.cause`, `.channels`, `.holder_fn`, `.holder_kind` (and `.tlvs`; gpio's refused position is its TLV 0x40). An unknown console stream is `no_connection`.
  - The analog's zero and scale are signed (i32).
  - USB: the vendor transport is the class 0xFF interface's bulk pair (not the first bulk pair); `open_host("usb:<unit id>")` finds the OEP probe whose USB serial is that unit id; unit_id is text (the fake: `esp32p4` / `esp32` / `rp2350` models, lowercase hex unit ids).
  - `target` (the first draft's re-exports) is gone: import the modules; README lists the public modules and the version pairing with the firmware.
- (JA) 凍結前の決定（oep-spec docs/v1-freeze-decisions.ja.md）を、fake とクライアントに一度に入れた:
  - `oep.fixture.capture` は `oep.fixture.logic` になった。I2C / SPI の target は標準の `oep.fixture.i2c-target` / `spi-target`（`fixture.I2cTarget` / `SpiTarget`。`esp32_targets` は無くなった）。status は分けたフィールド（`I2cStatus(state=, mode=, armed=, queued=, …)`）、`read_hw` は無くなり、`set_stretch` は `stretch`（op 0x07）。
  - 応答の並びは要素の前に長さを置く（core §2.3）: list、scan、connections、marks、capture の segments（`message.Reader.element()`、`message.element()`）。要素の知っているフィールドの後ろは飛ばす。
  - probe.config: スロットの錠は長さ付き（`lock_len`、0 は錠なし）。設定は、指すインターフェースの (name、instance、revision) と一緒に保存し、起動時に番号を読み替える。ほかのインターフェースを足したり動かしたりしても設定は効いたまま（fake: `saved_ids`）。storage は読めない理由を返す（`State.unreadable`）。項目のクラス（`Plan`、`Label`、`Idle`、`Slot`、`Bind`）はキーワード引数だけを取る。
  - rejected unavailable は core §4.3 の TLV を持つ: `host.Unavailable` の `.cause`、`.channels`、`.holder_fn`、`.holder_kind`（と `.tlvs`。gpio の断った位置は、その TLV 0x40）。知らない console の stream は `no_connection`。
  - アナログの zero と scale は符号付き（i32）。
  - USB: vendor の経路は class 0xFF のインターフェースの bulk の組（最初の bulk の組ではない）。`open_host("usb:<unit id>")` は、USB の serial がその unit id の OEP の probe を探す。unit_id は text（fake: model は `esp32p4` / `esp32` / `rp2350`、unit id は小文字の 16 進）。
  - `target`（最初の版の再輸出）は無くなった: モジュールを直接 import する。README に公開のモジュールと、firmware との版の組を書いた。

## 0.0.18
- (EN) No user-facing changes recorded.
- (JA) ユーザー向け変更の記録はありません。

## 0.0.17
- (EN) The fake's analog shares its pins with nothing (oep-if-capture §1.2): a plan that puts a logic capture, a fixture or a wire on an analog channel, or the analog on a pin another fn / a slot / a connection uses, is rejected unavailable, whichever comes second. A logic capture still listens on anything else.
- (JA) 偽の probe のアナログは、ピンを誰とも共有しない（oep-if-capture §1.2）。アナログのチャネルにロジックのキャプチャ、fixture、線を載せる plan も、ほかの fn・スロット・接続が使うピンにアナログを載せる plan も、後から来た方を rejected unavailable で断る。ロジックのキャプチャは、それ以外ではどのピンでも聞ける。

## 0.0.16
- (EN) No user-facing changes recorded.
- (JA) ユーザー向け変更の記録はありません。

## 0.0.15
- (EN) `CaptureGroup.wait()` keeps the lock alive while it waits too (`keepalive_s`).
- (JA) `CaptureGroup.wait()` も、待つ間ロックを保つ（`keepalive_s`）。

## 0.0.14
- (EN) No user-facing changes recorded.
- (JA) ユーザー向け変更の記録はありません。

## 0.0.13
- (EN) No user-facing changes recorded.
- (JA) ユーザー向け変更の記録はありません。

## 0.0.12
- (EN) `LogicCapture.force()`: waiting for the trigger, start now. `wait()` keeps the lock alive while it waits (`keepalive_s`, default 1 s): a trigger may come later than the lease.
- (JA) `LogicCapture.force()`: トリガを待っていれば、今すぐ始める。`wait()` は待つ間ロックを保つ（`keepalive_s`、既定 1 秒）。トリガはリースより後に来ることがある。

## 0.0.11
- (EN) The fake analog declares its 16-bit slots as bit 4 of the channels tag (bit i = 2^i, oep-if-capture §3.5).
- (JA) 偽の probe のアナログは、16 ビットの枠を channels の tag のビット 4 で宣言する（ビット i = 2^i、oep-if-capture §3.5）。

## 0.0.10
- (EN) Captures' times are ns on the probe's one clock with an uncertainty (oep-if-capture): `Segment.start_ns` / `start_uncertainty_ns` replace `start_us` (a segment is 33 bytes), the triggered event carries `trigger_ns`, and `Config.rate_measured` / `rate_ppm` say how sure the rate is. Analog captures: `AnalogCapture` (raw `values()`, the probe's `millivolts()`, `calibration()` - factory data and Vrefint, raw), with the answer read per channel (`zero`, `scale_nv`, `skew_ns`, `frontend`) and `reference`; `configure(frontends=)`. `CaptureGroup` binds tracks, starts them together with one trigger, and gives the group's `start_ns` and `trigger_ns`. The fake has them all (p4-x035: an analog on the ADC1 and a group), and describes a `chip`.
- (JA) キャプチャの時刻は、probe の 1 本の時計の ns と不確かさになった（oep-if-capture）: `start_us` は `Segment.start_ns` / `start_uncertainty_ns` になり（区画は 33 byte）、出来事 triggered は `trigger_ns` を持ち、`Config.rate_measured` / `rate_ppm` がレートの確かさを示す。アナログ: `AnalogCapture`（生の `values()`、probe の 1 次式の `millivolts()`、`calibration()`: 出荷時の値と Vrefint を生のまま）。答えはチャネルごとに読む（`zero`、`scale_nv`、`skew_ns`、`frontend`）、`reference` も。`configure(frontends=)`。`CaptureGroup` はトラックを束ね、1 つのトリガで一緒に始め、組の `start_ns` と `trigger_ns` を返す。偽の probe はこれらをすべて持つ（p4-x035: ADC1 のアナログと組）。core の describe の `chip` も出す。

## 0.0.9
- (EN) The fake probe has `oep.fixture.capture` (logic, `fake_capture`): one-shot, repeat (a ring and release), streaming (data pushes), level / edge triggers with a pretrigger, events, and a known waveform (sample i = the counter i, channel k = its bit k) in the profile's layout (P4: w 1-16; classic ESP32: w 8). A capture shares pins with other interfaces (it only listens). `Endpoint.pushes()` gives the frames the probe sends by itself; fake_serve sends them on the pty and TCP. `--capture-slipped` sets flags bit2 on every segment.
- (JA) 偽の probe に `oep.fixture.capture`（ロジック、`fake_capture`）を入れた: ワンショット、リピート（リングと release）、ストリーミング（データの push）、レベル / エッジのトリガとプリトリガ、出来事。取れる波形は決まっている（サンプル i = カウンタ i、チャネル k = そのビット k）。layout は profile のもの（P4: w 1〜16、classic ESP32: w 8）。capture はほかのインターフェースとピンを共有できる（聞くだけ）。`Endpoint.pushes()` が probe の送り出すフレームを返し、fake_serve は pty と TCP に流す。`--capture-slipped` は区画すべてに flags bit2 を立てる。
- (EN) The fake: an attach without pins joins the wire's one live connection, also on a wire whose pins the host chooses (oep-if-debug §1).
- (JA) 偽の probe: pins の無い attach は、その線の生きている接続が 1 つならそれに乗る。host がピンを選ぶ線でも同じ（oep-if-debug §1）。

## 0.0.8
- (EN) Wires whose pins the host chooses (oep-if-debug §1, role_channels): the fake takes any allowed free pair, skips the pairs whose pins something holds in a count-0 scan, holds a live connection's pins against plans, and has a profile for it (`rp2350-pins`). `Wire.scan()` goes through the whole list (at most 255 pairs a request; count 0 continues with the new skip TLV until tried = 0).
- (JA) host がピンを選ぶ wire（oep-if-debug §1、role_channels）: 偽の probe は、許された空いている組ならどれでも受け、count = 0 の scan では何かが持っているピンの組を飛ばし、生きている接続のピンを plan から守る。そのための profile（`rp2350-pins`）を足した。`Wire.scan()` は並びを最後まで回る（1 回 255 組まで。count = 0 は新しい skip の TLV で tried = 0 まで続ける）。

## 0.0.7
- (EN) `oep dump` names the wires' reset role (role 3) and no longer lists riscv-dm's clobbers (gone: the probe puts the registers back itself, oep-if-debug §4.5).
- (JA) `oep dump` は wire の reset の役（role 3）を名前で出し、riscv-dm の clobbers を出さなくなった（無くなった: probe が自分でレジスタを戻す、oep-if-debug §4.5）。
- (EN) `LogicCapture.to_sr` writes up to 16 channels (sigrok unitsize 2 above 8); it stopped at 8.
- (JA) `LogicCapture.to_sr` は 16 ch まで書く（8 ch を超えると sigrok の unitsize 2）。前は 8 ch まで。

## 0.0.6
- (EN) No default reset line (oep-if-debug §3): `Wire.attach_under_reset(channel, ...)` needs its channel, and `Wire.reset_channels()` reads the ones the probe allows (describe role_channels, role reset). The fake takes only those, and not one a plan holds.
- (JA) 既定の reset 線は無くなった（oep-if-debug §3）: `Wire.attach_under_reset(channel, ...)` は channel が必須。probe が許す channel は `Wire.reset_channels()` で読む（describe の role_channels の role reset）。偽の probe はそれだけを受け、plan が持つ channel は断る。
- (EN) The line's settings are the target's, passed by the host: `attach(..., idle_clock="low")` (rvswd, critical) and the probe.config slot's new max_speed / idle_clock fields (`oep config slot --max-speed --idle-clock`) for the probe's own attach. The slot item's layout changed.
- (JA) 線の設定は target のもので、host が渡す: `attach(..., idle_clock="low")`（rvswd、critical）と、probe が自分で attach するための probe.config のスロットの max_speed / idle_clock（`oep config slot --max-speed --idle-clock`）。スロットの項目の形が変わった。
- (EN) The fake probe keeps a plan the settings put in (oep-core §8): plan_release leaves it (n = 0 included) and plan_apply naming its fn is rejected unavailable.
- (JA) 偽の probe は、設定が入れた plan を設定のものとして扱う（oep-core §8）: plan_release はそれを解かず（n = 0 でも）、その fn を挙げた plan_apply は rejected unavailable。

## 0.0.5
- (EN) USB (python-libusb1) streams close cleanly and at exit: the IN transfers are cancelled and taken back by the event thread before the handle and the context go. A program that ended without closing the link hung at exit, or libusb aborted.
- (JA) USB（python-libusb1）の stream は、終了時にも片付いて閉じる: IN の転送を取り消し、event thread が取り戻してから handle と context を閉じる。link を閉じずに終わったプログラムが終了で固まったり、libusb が abort したりしていた。
- (EN) `core.plan_apply` refused as unavailable names what holds the pins when the settings say so (`core.PinsTaken`: another fn's saved plan, a slot).
- (JA) `core.plan_apply` が unavailable で断られたとき、設定から分かれば何がピンを持っているかを示す（`core.PinsTaken`: 別の fn の保存した plan、スロット）。
- (EN) Fake probe: a read from the last mark of a kind that is not there starts now, not at the oldest byte (oep-if-common §1.2).
- (JA) 偽の probe: その kind のマークが無いときの「最後のマークから」の read は、一番古い位置ではなく今から（oep-if-common §1.2）。

## 0.0.4
- (EN) Fake serial port: a candidate that is only its 0x00 is not raw after the 200 ms gap (the probe firmware had the same bug: a stray 0x00 reached the target's console).
- (JA) 偽のシリアルの口: 0x00 だけの候補は、200 ms の後も生のバイトにしない（probe の firmware にも同じバグがあり、target のコンソールに 0x00 が届いていた）。
- (EN) `oep config slot` takes the probe's only RISC-V wire when `--wire` is left out (a SWIO-only probe needed `--wire swio`).
- (JA) `oep config slot` は `--wire` を省くと、probe の唯一の RISC-V の線を使う（SWIO だけの probe で `--wire swio` が要っていた）。
- (EN) `oep config plan <probe> <fn | name#k> ROLE=CH ...` (role names from the registry: rx, tx, line...), `oep config label` and `oep config idle`.
- (JA) `oep config plan <probe> <fn | 名前#k> ROLE=CH ...`（role の名前は registry から: rx、tx、line など）、`oep config label`、`oep config idle`。

## 0.0.3
- (EN) `oep_client.config` (oep.probe.config: slots, binds, plan / label / idle items, get / set / save / erase, the live slot and bind state) and the `oep config show | slot | bind | remove | save | erase` command. An English README.md, also the PyPI page.
- (JA) `oep_client.config`（oep.probe.config: スロット、bind、plan / label / idle の項目、get / set / save / erase、スロットと bind の今の状態）と、`oep config show | slot | bind | remove | save | erase` の命令。英語の README.md（PyPI のページも）。

## 0.0.2
- (EN) Breaking: the modules move from `oep_client.v1.*` to `oep_client.*` (`from oep_client import link`, `python -m oep_client`, `python -m oep_client.fake_serve`). The protocol revision stays in the registry and confirm.
- (JA) 破壊的変更: モジュールを `oep_client.v1.*` から `oep_client.*` に移した（`from oep_client import link`、`python -m oep_client`、`python -m oep_client.fake_serve`）。プロトコルの revision は registry と confirm が持つ。
- (EN) `fake_serve --uart-plan` puts the first fixture UART's RX / TX plan in at boot (as a saved config), and `--uart-rx TEXT` feeds its RX every `--every` ms once configured.
- (JA) `fake_serve --uart-plan` で最初の fixture UART の RX / TX の plan を起動時に入れる（保存した設定として）。`--uart-rx TEXT` で、configure の後、その RX に `--every` ms ごとに文字を入れる。

## 0.0.1
- (EN) First beta on PyPI (`pip install oep-client-python`, imported as `oep_client`). OEP v1 host: serial ports always COBS with the probe's raw bytes skipped as noise and opened exclusively (TIOCEXCL), USB vendor bulk / HID and TCP (a broker) with length frames, all under one `Host` (`link.open_host(target)`); the session rules (owner, resend with the same corr, `core.take` for the lock); the standard interfaces; `Host.on_capture` for run recorders; a fake probe (`oep_client.v1.endpoint`) and `python -m oep_client.v1.fake_serve` (pty / TCP) as the working spec for other hosts' tests.
- (JA) PyPI での最初のβ版（`pip install oep-client-python`、import は `oep_client`）。OEP v1 の host: シリアルの口は常に COBS（probe の生のバイトは雑音として捨てる、排他で開く TIOCEXCL）、USB の vendor bulk / HID と TCP（ブローカー）は長さつきのフレームで、どれも同じ `Host`（`link.open_host(target)`）。セッションの規則（owner、同じ corr での送り直し、ロックの取り方 `core.take`）、標準インターフェース、記録の受け口 `Host.on_capture`。ほかの host の試験のための「動く spec」として、偽の probe（`oep_client.v1.endpoint`）と `python -m oep_client.v1.fake_serve`（pty / TCP）。
