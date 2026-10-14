# capture-groupの論理操作検査

`CaptureGroupChecks(checks, adapter).run()`は明示したSPECのcapture §4.1、plan §2、core §4.3/4.4/6/8/9を
第三段階で確認する。`Checks(send, registry, unit)`と`SampleCaptureGroupAdapter(source)`を渡す。
公開checker/adapterは私的ベンチ・兄弟checkout・設備設定を探索しない。
前提と登録op宣言（12 fn）を含む53項目。共通宣言97項目とgroup/track metadata8項目は別に適用する。

- 未結合のstate 0、start前の時刻sentinel（全bit1）と世代0。startのcause 6拒否、stopと空bindのno-op。
- bind順序、同じリストの再指定のno-op（取得中も状態・データを保つ）、bindが世代を変えないこと。
- bindの拒否と非変更。重複fnはmalformed、tracksに無いfnはunsupported（印0、fn TLV）。
  未設定/state 1・4・6以外（単独取得中）/mode不一致/即時でないtriggerはcause 6、別の組に束ねたtrackはcause 4、
  同時資源の不足（このfixtureでは異なるrate）はcause 2。causeと該当trackのfn TLVを読み返す。
- bindはv1のTLVを持たない。知らないcritical TLVは受け取ったままのtagでunsupported、critical bitの無いTLVは無視する。
  start/stop/statusの要求TLVも同じ。
- 64 byteの経路でstart応答（18+6n）が収まらない8 trackのbindをunsupported（印0）で断り、収まる7 trackは受け付けて開始できる。
- 結合中の単独configure/start/stop/force、plan_apply/plan_releaseをcause 4で拒否。同じplan集合の再指定はno-op。
  queryは結合中・取得中でも候補を適用せず、他trackやbindを変えない。
- 取得中（state 3）の組は空bind/別リストをcause 6で断る。完了したtrack（state 4）は別リストへ束ね直して開始できる。
- start前提は全trackで先に確認し、取得中の再startはcause 6。start応答のbind順のfn一覧、組と各trackの独立した世代、
  status readbackを確認する。最後の世代0xFFFFFFFFは使え、その後のstartは組でもtrackでもcause 2で断る。
  bindし直しても世代を継続する。
- 取得したADC/logicの生データ、完成区画の共通開始時刻と即時の区画（trigger_index 0xFFFFFFFF、flags 0）。
- group stopで全trackを止め、各途中区画をshortとして残す。
- repeatの個別pauseをgroup state 3とする。releaseはtrackごとで、peerの区画/bytesを変更しない。
- ADC/logicそれぞれの取得失敗でgroup state 6、peerをstopし、完成区画を保持する。
- bind/start/stop再送の二重副作用。end/lease expiry/強制openでbindとplanを解放し、
  世代を保ちながら未設定状態へ戻す。取得中にもpinの電気状態を変えていないことを観測する。

独立`CaptureGroupModel`はADC/logicのtrackとgroupを持つ。
独立`Analog`/`Logic`の設定・encoder・区画リングを再利用し、groupのbind/start/寿命は別に実装する。
既存`virtual_bench_capture.VirtualGroup`とHostのcapture decoderを取り込まない。
ADCのoracleは`AnalogPayloadChecks`、logicは割当bitを独立に読み、生の値と照合する。

fixtureのfnはplan=1、ADC=2、logic=3、group=4。ADC role1/3をchannel0/1、
logic role0/1をchannel2/3へ割り当てる。双方の最大チャネル数は2。
補助としてfn 5に別のgroup（cause 4の相手）、fn 6〜12に1 role（channel fn−2）のlogicを置き、
`adapter.auxiliary`で明示する。core describeのchannelsは16。
max_frameは256 byte（frame検査だけ明示の`frame(64)`で64）、one-shot/repeat、track forceあり、
group/track subscribeなし。取得資源は同レートの組だけを扱い、異なるレートはcause 2で拒否する。
checkerの取得刺激は1000 Hzで、ADCの12 bit値とlogicの0/1値を明示したpaired rowを渡す。
この制限をOEPの一般的なgroup仕様に追加するものではない。

`feed(rows)`の各要素は `{2: [ADC role1, role3], 3: [logic role0, role1]}`。
logical sample clockを1行ずつ進める。
`advance`/`fail(fn,error)`/`seed(group,adc,logic)`/`frame(limit)`は明示刺激。
`invalidate(fn)`はstart前提の破れを注入し、他trackを先にstartしないことを検査する。
wire交換・group/track状態・データ・区画・pin・clockをadapter traceへ記録する。

単体89件。39 caseのwire応答故障、35の意味的故障、3種の再送二重副作用を検出する。
意味的故障の検出は、検査の`Violation`で落ちることまで確認する。
前提不足・不正刺激と、fixtureのchannel広告が実装に一致することも確認する。
metadataの構造が正しいことと、fixtureがその広告に対応することは別の検査である。

`full_conformance`はfalse。subscribe/stopped通知と各trackの通知、streaming、
他の資源やgroup構成、start自体の開始失敗（completed failed）、serial保持境界、
並行性・永続config、物理同期/skew/精度とproduction Hostは未検査。
有限な同期fixtureの成功を全group操作・実機適合とは扱わない。

```sh
uv run pytest -q tests/test_conformance_capture_group.py tests/test_conformance_analog.py tests/test_conformance_logic.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_group.py
```

異なるactual_rateのtrackを即時に一緒に始めた初回区画は、[group時刻検査ガイド](capture-group-geometry-conformance.ja.md)の
別checkerでも確認する。生の観測payloadと既知のsample起点を明示する検査であり、
この同レートの独立操作fixtureの取得能力を広げるものではない。

異レートの独立取得は[取得timelineガイド](capture-timeline-conformance.ja.md)の別fixtureで確認する。
同レート操作fixtureの資源制限はそのまま維持する。
