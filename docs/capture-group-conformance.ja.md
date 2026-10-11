# capture-groupの論理操作検査

`CaptureGroupChecks(checks, adapter).run()`は明示したSPECのcapture §4.1、plan §2、core §6/8/9を
第三段階で確認する。`Checks(send, registry, unit)`と`SampleCaptureGroupAdapter(source)`を渡す。
公開checker/adapterは私的ベンチ・兄弟checkout・設備設定を探索しない。
前提と登録op宣言を含む43項目。共通宣言33項目とgroup/track metadata8項目は別に適用する。

- 未結合のstate 0、start拒否、stop/force/空bindの非変更、start前の時刻sentinel。
- bind順序、重複/未宣言/未設定/モード不一致/他trackのtrigger/pretrigger不足/資源不足の拒否と非変更。
  拒否のcauseと、該当するtrackのfn TLVを読み返す。bind TLVの幅、critical同等性、first-wins。
- 結合中の単独configure/start/stop/force、plan_apply/plan_releaseをcause 4で拒否。
  queryは結合中・取得中でも候補を適用せず、他trackやbindを変えない。
- start前提は全trackで先に確認。start応答のbind順のfn一覧、組と各trackの独立した世代、
  それぞれのFFFFFFFF→1、status readbackを確認する。bindし直しても世代を継続する。
- 取得したADC/logicの生データ、完成区画の共通開始時刻、threshold待ちから全trackの取得開始。
  同レートの2 sample prehistory、共通trigger_nsと各trigger_index、forceでtrigger_fn=0を確認。
- group stopで全trackを止め、各途中区画をshortとして残す。
- repeatの個別pauseをgroup state 3とする。releaseはtrackごとで、peerの区画/bytesを変更しない。
- ADC/logicそれぞれの取得失敗でgroup state 6、peerをstopし、完成区画を保持する。
- bind/start/stop再送の二重副作用。end/lease expiry/強制openでbindとplanを解放し、
  世代を保ちながら未設定状態へ戻す。取得中にもpinの電気状態を変えていないことを観測する。

独立`CaptureGroupModel`はADC/logicの2 trackとgroupを持つ。
独立`Analog`/`Logic`の設定・encoder・区画リングを再利用し、groupのbind/start/trigger/寿命は別に実装する。
既存`virtual_bench_capture.VirtualGroup`とHostのcapture decoderを取り込まない。
ADCのoracleは`AnalogPayloadChecks`、logicは割当bitを独立に読み、生の値と照合する。

fixtureのfnはplan=1、ADC=2、logic=3、group=4。ADC role1/3をchannel0/1、
logic role0/1をchannel2/3へ割り当てる。双方の最大チャネル数は2。
max_frameは256 byte、one-shot/repeat、group/track forceあり、subscribeなし。
取得資源は同レートの組だけを扱い、異なるレートはcause 2で拒否する。
checkerの取得刺激は1000 Hzで、ADCの12 bit値とlogicの0/1値を明示したpaired rowを渡す。
この制限をOEPの一般的なgroup仕様に追加するものではない。

`feed(rows)`の各要素は `{2: [ADC role1, role3], 3: [logic role0, role1]}`。
logical sample clockを1行ずつ進め、sourceの条件が立つまでは各trackのprehistoryを保持する。
`advance`/`fail(fn,error)`/`seed(group,adc,logic)`は明示刺激。
`invalidate(fn)`はstart前提の破れを注入し、他trackを先にstartしないことを検査する。
wire交換・group/track状態・データ・区画・pin・clockをadapter traceへ記録する。

単体72件。37 caseのwire応答故障、21の意味的故障、3種の再送二重副作用を検出する。
前提不足・不正刺激と、fixtureのchannel広告が実装に一致することも確認する。
metadataの構造が正しいことと、fixtureがその広告に対応することは別の検査である。

`full_conformance`はfalse。異なるレートのP_k切り上げ/最寄りsampleの丸め、streaming/購読/通知、
他の資源やgroup構成、早いtriggerの履歴不足、即時triggerを持つsource指定、serial保持境界、
並行性・永続config、物理同期/skew/精度とproduction Hostは未検査。
有限な同期fixtureの成功を全group操作・実機適合とは扱わない。

```sh
uv run pytest -q tests/test_conformance_capture_group.py tests/test_conformance_analog.py tests/test_conformance_logic.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_group.py
```
