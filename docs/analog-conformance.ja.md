# analog captureの論理操作検査

`AnalogChecks(checks, adapter).run()`は、明示したSPECのcapture §1.2/2/3/3.7、plan §2、core §4.4/6/8/9を
第三段階で確認する。`Checks(send, registry, unit)`と`SampleAnalogAdapter(source)`を渡す。
公開checkerとadapterは私的ベンチ・兄弟checkout・設備設定を探索しない。
前提と標準op宣言を含む46項目。共通宣言とcapture metadataは別checkerで確認する。

既存`LogicChecks`のwire交換・世代・データ寿命・資源寿命の検査手順を再利用し、
analogの成功payloadと生データは`AnalogPayloadChecks`で独立に読む。
Hostのcapture decoderや仮想probeのencoderを正解の生成に使わない。
複数trackの結合は[capture-group操作ガイド](capture-group-conformance.ja.md)の別checkerで確認する。

- planなしのconfigure/query/start、SIDの要否、設定契約・幅・0値・critical同等性・raw tag・first-wins。
  triggerの無いpretrigger、宣言しないmode 3のconfigure/queryをそのtagのunsupportedで断ること。
- rateの範囲、samplesの容量切り下げ、repeat区画数の上限への切り下げと省略時の値、triggerのtype/role/値、pretrigger。
- 応答はactual_rate/layout、mode 1/2のactual_samples、mode 2のactual_segments、roleごとのscaleを必須とする。
  skew・frontend_used・referenceは省いてよく、返したときだけ形とroleを確かめる。
- planありstate 0〜6で、queryが取得設定・世代・data・区画・pinを変更しないこと。
- role 1/3、layout.order=[3,1]、16 bit枠のoffset 2/bits 12。未使用bitを1にした生データを
  role番号とlittle endianで読み返す。負scaleや較正を保存する生の値に適用しない。
- 上向き/下向きthresholdの待ちと2 sampleのprehistory、完成区画のtrigger_indexと世代。
- frontendの選択/省略、幅、未宣言値、未計画role、同じroleの重複拒否。拒否時は取得を変えない。
  選択の結果はscaleの符号で確かめ、frontend_usedは返したときだけ照合する。
- startの応答はgeneration(u32)だけ。初回世代、FFFFFFFFの後のstartはunavailable cause 2で断り、configure後も再利用しない。
  旧世代のread/release拒否、再start/configureのデータ消去。
- readはmax/lenがu16。max/more/gap、SID 0での非消費読み出し。write_pos以上はwrite_pos・gap 0・len 0。
  全区画を解放して読めるデータが無ければwrite_pos・gap 1・len 0。max=0も同じ位置とgapの規則に従う。
- 区画ページング、repeat満杯/pause、inclusive release/resume、残る区画とdata、再開のgap。
- short stop、error時の不完全区画破棄と完成区画保持、error後start。
- start/release再送の二重副作用、plan置換/release/end/expiry/強制openによる資源閉鎖。
- calibration照会の非変更、factoryの保持、最後のstartに対応するfixtureのreference値と時刻。
- calibrationの応答がmax_frameにちょうど収まればfactoryのrawを全部返す。1 byte超えれば固定部分の印0の
  unsupportedで断り、rawの切り詰め・vrefintの省略・partialで成功にしない。startでvrefintが増えて超えた場合も同じ。

独立`AnalogModel`はone-shot/repeat、queryあり、force/subscribeなしのfixture。
rate 100〜1000 Hzはexact、samples上限16、repeat区画上限2、pretrigger上限8。describeのmode 1はmax_segments 0、
rate_rangeはmin/max、frontendは番号と入力範囲だけ。
2 frontendと2 ADC channelを持ち、role 1/3をchannel 0/1へ割り当てる。
max_frameは256 byteと明示する。2チャネルの必須configure応答全体が収まるサイズであり、
すべてのprobeにこのサイズを要求するものではない。calibrationは収まらなければunsupported（印0）で断る。

`feed`へ渡す各行はplan role番号の小さい順のu12値。取得した数に応じて分数周期を累積し、
logical msへ変換する。`advance`/`fail`/`seed`は明示したclock・取得失敗・世代境界の刺激。
`factory`はfixtureが保持するfactory rawの長さ（0〜4096 byte）を決める刺激で、設定したrawを返す。
modelの共通取得状態機械は独立`LogicModel`と共有するが、ADCの設定・trigger・encoder・校正は別実装。
既存ベンチの`virtual_bench_capture`やHost decoderは取り込まない。
referenceはstartのlogical clockで同期測定したfixture値であり、一般のprobeの測定遅延を規定しない。

単体82件。42 caseへのwire応答故障、24の意味的な故障、再送の二重副作用2件を検出する。
意味的な故障には、世代の1への巻き戻り、scaleの欠落、calibrationの切り詰め/vrefint省略/partial、
読めるデータが無いときのgap欠落、宣言しないmode 3の受理を含む。任意の応答行を省いたfixtureの合格4件、
不正刺激と前提不足も確認する。共通宣言/metadata、旧logic、analog payloadの回帰と併せて実行する。

`full_conformance`はfalse。group、streaming/通知、optional force/subscribe、
他のADC形/rate/trigger境界・基準電圧測定方式、serial wrap/保持境界/並行性/永続config、
物理ADC精度/skew/clock、production Host integrationは未検査。
calibrationを持たないprobeの空成功の構造はpayload checkerで許容する。
このfixtureのvrefint値や存在を一般のprobeへ要求する検査ではない。

```sh
uv run pytest -q tests/test_conformance_analog.py tests/test_conformance_logic.py tests/test_conformance_analog_payload.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_analog.py
```
