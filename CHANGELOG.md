# Changelog / 変更履歴

## Unreleased
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
