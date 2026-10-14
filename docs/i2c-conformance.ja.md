# I2C targetの論理検査

`I2cChecks(checks, adapter).run()` は選択SPECのfixture §3、plan §2とcore §6/8/9を第三段階で検査する。
`Checks(send, registry, unit)` と `SampleI2cAdapter(source)` を明示して渡す。
公開側は私的ベンチ・設備設定・隣接リポジトリを探索しない。

前提と標準op宣言を含む35項目。別に第二段階の共通宣言17項目とI2C metadata 5項目を適用する。

- 未設定ではどのアドレスにもACKしない。plan前configureと未設定read_rx/preload_txを拒否する。
- role 1/2を各1つ、別のchannelに割り当てる。満たさない組はunsupported（role_assignmentのtag）、同じ(fn, role, channel)の重複はmalformed。
- state 1で同じ割り当てのplan_applyは何も変えず、割り当ての変更はunavailable cause 6で、plan、queue、pinsを変えない。
- configureの初期化失敗はcompleted failedで、state 0へ戻り列・置き場・累計を消し、stretchを保つ。read_rxはcause 6、新しいconfigureで回復する。
- addressの7 bit幅と予約範囲、合法境界、選択アドレス以外からのアクセスでqueueを消費しないことを確認する。
- STARTからSTOPまたは次のSTARTまでのwriteを1フレームとしてFIFOで返す。任意timestampは存在時だけ幅と境界時刻を確認する。
- address-only writeは数えない。max_lengthの超過はprefixを残し、queueあふれは新しいフレームを捨てる。
  両方が起きてもerrorsは1回だけ増え、捨てたフレームをrx_framesへ数えない。
- preloadはFIFO。短いreadでもslotを消費し、長いreadは同じslotの後をFFで埋め、次のslotを混ぜない。
  満杯時はcause 2で拒否し、slotを消費した後はまたpreloadできる。
- configureはRX/TX/累計を初期化し、stretchを保つ。stretchなしのfixtureはunknown_operation。
  stretchありではstate 0、上限、writeのdata byteだけ・readのaddress後への適用、0による無効化を確認する。
- 内部pullupは宣言に従う。logical pinはopen-drainの解放状態とし、push-pull highを検出する。
- read_rx/preload/configureの同一要求再送は副作用を繰り返さない。
- release/end/expiry/forceで未設定に戻し、queue・累計・stretchを捨て、pinsをidleへ戻す。
  force後の旧要求は新ownerのtargetを変更できない。statusはSID 0で読め、他操作はsessionを要求する。

独立`I2cModel`はmax_length 8、queue_depth 3、max_clock 400000 Hz、stretch上限1000 µsの論理fixture。
role候補はchannel 0/1、idleは入力。明示logical ms時計と完了済みcontroller transaction刺激を用いる。
stretch/内部pullupの有無をconstructorで明示し、4構成を検査する。
ACK・stretch回数と終了時刻、queue/pin/journal、wire交換を保存する。
この刺激は実バスのエッジ・波形・実時間を再現するものではない。

単体64件で4構成、共通宣言/metadata、各caseのwire誤応答、FIFO/二重errors/初期化漏れ/slot再利用/誤timestamp/誤駆動、
再送での二重消費、任意timestampの省略、前提不足・不正刺激を確認する。
`full_conformance` はfalse。物理I2C、永続config、他pin/channel_group、255以上のqueue/u32 counter境界、
並行controller/writerとdeferred response、production Host統合は未検査。

```sh
uv run pytest -q tests/test_conformance_i2c.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_i2c.py
```
