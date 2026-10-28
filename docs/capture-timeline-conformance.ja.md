# 異なるレートのgroup取得検査

`CaptureTimelineChecks`は、明示したSPECのcapture §2/3.2/3.4/4.1とcore §10に基づく第三段階の検査。
`SampleCaptureTimelineAdapter`で独立`CaptureTimelineModel`の時計と通知の取り出しを指定する。
私的ベンチ、endpoint、兄弟checkout、既存Hostのcapture decoder、他のcapture checkerは取り込まない。
layoutはconfigureの応答から独立に解き、生データを既知の波形と照合する。

このfixtureはplan=1、ADC=2、logic=3、group=4。
ADC role1/3とlogic role0/1を別channelへ割り当て、即時triggerのone-shotとstreamingを宣言する。
groupはv1どおり即時の一括開始だけで、trigger・pretrigger・forceを持たない。
各trackはgroupのstart_nsを起点に、独立した有理数のgridでsampleを取る（index 0が開始時刻）。
`advance_to(ns)`は起動からの論理時刻、`drain()`はその時点までに作った通知frameを取り出す。
波形はsample index iに対し、ADC role1=(17i+3) mod 4096、role3=(4095−29i) mod 4096、
logic role0=i mod 2、role1=⌊i/3⌋ mod 2。刺激用の制御であり、OEP wireへ新しいopを追加するものではない。

24構成を検査する。

- modeはone-shot（16 sample）とstreaming。bind順は(ADC, logic)と(logic, ADC)。
- 先頭/2番目のtrackのrateは1000/333、333/1000、1000/500 Hz。
- 開始時刻は起動直後と1234567 ns後（ms境界に揃わないstart_ns）。

one-shotでは次を確認する。

- startの応答はstart_ns(u64)、generation(u32)、n、n×(fn, generation)で、世代は1ずつ進み一周しない。
  groupとtrackのstatusで同じ値を読み返す。
- 取得途中のwrite_posは到来したsampleの確定byteだけを数える（logicの未確定な末尾byteを含めない）。
- readは、write_pos以上の位置（write_pos、+1、+4096）へposition=write_pos、gap 0、len 0で答える。
  max=0も空の成功で、位置とgapは通常のreadと同じ。more bitはwrite_posまでの残りと一致する。
  one-shotの保持データは位置0からgapなしで読め、旧世代のreadはcause 6で断られる。
- 完了後は各trackに1区画(serial 0, position 0, 16 sample, start_ns=group start_ns, trigger_index 0xFFFFFFFF, flags 0, 世代)。

streamingでは購読前のgroup startがcause 6で断られることを確かめ、両trackを購読(min_bytes 0, max_delay 0)して開始する。
2/9/23/41/58 msで通知を取り出し、58 msでgroupを止めて残りを受け取る。

- data frameはmax_frame以内で、毎frameにTLV 0x01 generationと0x02 segment_info（24 byte）を持つ。
- segment_infoのposition/start_ns/不確かさはsegmentsの区画と一致し、start_nsはtrackのgrid上の時刻。
  samplesは1以上で減らず、そのframeを作った時点までに到来したsample数を超えない。
- frameのdataは1区画の中だけにあり、区画の境目をまたがない。区画が終わるまでは確定byteだけを送る。
- 受け取ったpositionは0から隙間なく続き、停止後のwrite_posで終わる（このfixtureは落とさない）。
- 区画は隙間なく続き、短い区画は最後だけ。区画の出来事は送らず、stoppedはreason 1、error 0、世代で1回。
- readは通知で送ったデータを返さなくてよい。gapで進めた結果、読めるデータが無ければposition=write_pos、gap 1、len 0。
- 停止までに取ったsample数は到来数と一致し、組み立てた区画のbyteを既知の波形と照合する。

いずれもsession end後にbind、plan、購読、通知、取得データを解放し、pinをidleへ戻し、世代を保つことを確認する。

単体79件は正常24、wire応答故障24、track故障16、group故障4、宣言2、不正刺激7、前提不足2。
track故障は未到来sampleの計上、未確定byte、生データ、区画時刻、readの位置/gap/max=0の誤り、
区画をまたぐframe、segment_infoの欠落・sample数・時刻、generation TLV、streamingの区画出来事を注入する。
group故障は世代0の応答、購読なしのstart受付、終了後の購読残留、start_nsのずれを注入する。
全24構成をまとめて報告する。

`full_conformance`はfalse。既知の2 track/即時開始の論理fixtureを検査する。
repeat/release、streamingの送信余地不足や購読喪失による落とし、他のgroup構成・ADC layout/skew、
保持境界、非同期取得中の競合、物理clockの精度とproduction Hostは別途必要。
時刻照合はこのfixtureの整数ns表現に対するもので、物理probeの精度保証や報告uncertaintyに基づく許容幅ではない。

停止・エラー・世代の使い切り・start失敗・状態表・session寿命は[別の寿命checker](capture-timeline-lifecycle-conformance.ja.md)で確認する。

```sh
uv run pytest -q tests/test_conformance_capture_timeline.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_timeline.py
```
