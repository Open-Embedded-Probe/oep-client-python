# Changelog / 変更履歴

## Unreleased
- (EN) Breaking: the modules move from `oep_client.v1.*` to `oep_client.*` (`from oep_client import link`, `python -m oep_client`, `python -m oep_client.fake_serve`). The protocol revision stays in the registry and confirm.
- (JA) 破壊的変更: モジュールを `oep_client.v1.*` から `oep_client.*` に移した（`from oep_client import link`、`python -m oep_client`、`python -m oep_client.fake_serve`）。プロトコルの revision は registry と confirm が持つ。
- (EN) `fake_serve --uart-plan` puts the first fixture UART's RX / TX plan in at boot (as a saved config), and `--uart-rx TEXT` feeds its RX every `--every` ms once configured.
- (JA) `fake_serve --uart-plan` で最初の fixture UART の RX / TX の plan を起動時に入れる（保存した設定として）。`--uart-rx TEXT` で、configure の後、その RX に `--every` ms ごとに文字を入れる。

## 0.0.1
- (EN) First beta on PyPI (`pip install oep-client-python`, imported as `oep_client`). OEP v1 host: serial ports always COBS with the probe's raw bytes skipped as noise and opened exclusively (TIOCEXCL), USB vendor bulk / HID and TCP (a broker) with length frames, all under one `Host` (`link.open_host(target)`); the session rules (owner, resend with the same corr, `core.take` for the lock); the standard interfaces; `Host.on_capture` for run recorders; a fake probe (`oep_client.v1.endpoint`) and `python -m oep_client.v1.fake_serve` (pty / TCP) as the working spec for other hosts' tests.
- (JA) PyPI での最初のβ版（`pip install oep-client-python`、import は `oep_client`）。OEP v1 の host: シリアルの口は常に COBS（probe の生のバイトは雑音として捨てる、排他で開く TIOCEXCL）、USB の vendor bulk / HID と TCP（ブローカー）は長さつきのフレームで、どれも同じ `Host`（`link.open_host(target)`）。セッションの規則（owner、同じ corr での送り直し、ロックの取り方 `core.take`）、標準インターフェース、記録の受け口 `Host.on_capture`。ほかの host の試験のための「動く spec」として、偽の probe（`oep_client.v1.endpoint`）と `python -m oep_client.v1.fake_serve`（pty / TCP）。
