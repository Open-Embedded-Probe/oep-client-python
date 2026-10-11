# アナログ成功payloadと生データの検査

`AnalogPayloadChecks(checks)`は選択SPECのcapture §1.2/3.2/3.3/3.8を第三段階で確認する。
callerはconfigure/queryの成功payload、calibrationの成功payload、取得データと既知の生サンプル、
planのroles、describeのfrontend番号、modeとcore max_op_msを明示する。
layout.orderはplanのrole番号そのもの。飛び番も扱い、生サンプルの各行はrole番号の小さい順で渡す。
例えばroles=(1,3)でorder=[3,1]なら、各行の2番目の値が最初の枠に入る。
公開checkerは設備・私的ベンチ・隣接SPECを探索せず、Hostのcapture decoderも使わない。

wire操作・世代・データ寿命の検査は[analog操作ガイド](analog-conformance.ja.md)の
`AnalogChecks`でこのpayload検査と組み合わせる。

`run(configure_payload=..., calibration_payload=..., data=..., expected_samples=...,
roles=..., frontends=..., mode=..., max_op_ms=...)`は3項目を返す。
成功解決・wire幅/経路・SID・資源寿命はcallerの別検査で確認する。payloadを渡しただけで実行の適合証明にはならない。

- actual_rateの正のnum/den、layoutのs=8/16/32、b=1〜31、o+b≤s、Cとorderの並べ替えを確認する。
- modeで条件付けたactual_samples/actual_segments、blocking_ms≤core max_op_msを確認する。
- scaleとfrontend_usedはplanのroleごとに1つ、skewは0なら省略可能。未知role・重複roleを検出する。
  zero/scaleはi32。負のscale、コード範囲外のzero、scale 0を追加条件で断らない。
- referenceの幅とsource/howを確認する。mvの0を除く規則を創作しない。
- factoryはfrontendごとに1つ（0xFFはfrontendに依らない）、schemeの1〜64 byteと字種、raw_len/u16とrawの一致を確認する。
  rawの意味はschemeの定義に委ね、空rawも許容する。calibration全体が空の成功も許容する。
- vrefintは存在時だけ16 byteを確認する。未知の非critical応答TLVを無視し、非反復TLVは最初を使う。
- dataはN·C·s/8 byte。little endian枠のo/bだけを読み、orderへ従って既知の生サンプルと照合する。
  未使用bitは0/1のどちらも許容し、factory値やscaleを生データへ適用しない。

単体ではs/o/b、正順/逆順、未使用bitを0/1にした16構成と、構造・必須情報・enum・role・校正・生データの故障を検査する。
Host decoderの読み出し、負scaleのクリップと入力ピン換算も既知値と比較する。
既存VirtualCaptureの実際のsuccess payloadとraw取得にも適用し、校正schemeの禁止字種を検出した。

`full_conformance`はfalse。取得/拒否/再送/世代/trigger/通知/資源寿命、全analog操作、capture-group、
schemeごとのraw解釈と物理reference/skewの保証は未検査。schemaの検査だけでこれらを完了扱いにしない。

```sh
uv run pytest -q tests/test_conformance_analog_payload.py tests/test_virtual_bench_capture.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_analog_payload.py
```

## 既存仮想ベンチの設定要求と状態不変

`test_virtual_bench_capture_requests.py`はwire bodyを渡し、frontendの指定/幅/未宣言値/重複、
rate/samples/mode/triggerの拒否、query成功時の非適用を確認する。
拒否前後でcapture/config/data/segments/plan/GPIOを比較する。要求を受けたcoreのlease/cacheまで
不変と要求する検査ではない。coreでは正当な更新や要求履歴の保存がありうる。

区画満杯のstate 5はconfigureをunavailable・cause 6で拒否し、積んだ区画とdataを消さない。
logic/analog双方を確認する。planありのstate 5でもqueryを許可する規則はSPEC §3.3と
`test_virtual_bench_capture_query.py`で確認する。
既存仮想endpointのinterface回帰であり、そのlegacy coreを現在の独立core適合fixtureと同一視しない。

```sh
uv run pytest -q tests/test_virtual_bench_capture_requests.py
```

## 既存仮想captureの世代とデータ寿命

`test_virtual_bench_capture_lifetime.py`はlogic/analogの再startとu32世代境界、旧世代read/releaseの
拒否時不変、one-shot releaseの非適用、repeatのinclusive解放を確認する。
区画を解放しないreleaseは満杯のまま。実際に空きができたときだけ再開し、gap、通しpositionと
再開時のmodel clockを反映したstart_nsを保つ。最終空き区画が完了した時点でstate 5へ入る。

stopはすでに取った途中sampleを短い区画として確定し、空の区画を作らない。
errorは完了済み区画/dataを取り消さず、次startで世代/data/flagsを更新する。
error後のstopはdataを保ったままstate 1へ戻す。group stopの短い区画も両trackで検査する。
仮想modelの論理時刻の検査であり、実機clock精度や全group契約の証明ではない。

```sh
uv run pytest -q tests/test_virtual_bench_capture_lifetime.py
```

`test_virtual_bench_capture_resume_calibration.py`はrepeatのpause/release/resumeを3回繰り返し、
残った区画のwire recordと生データ、世代、元のtrigger時刻を保持することを確認する。
logic/analog、3種類の分数rate、1/2区画の解放を使い、再開後の最初の区画だけにgapを要求する。
既知波形のcallbackで、モデルの現在時刻より先のチャネル値を読んでいないことも確認する。

同じ検査でcalibrationのvrefintが最後のstartから照会時刻までの測定を表すことを確認する。
3モード、単独/group start、即時/trigger待ち、再startを含む。
このfixtureはstart時に同期的に基準電圧を測り、最初のチャネルsampleはその5µs後に始める。
そのためvrefintのnsと区画のstart_nsは同じ値とは限らない。
これはfixtureの測定モデルであり、一般のprobeに同じ遅延や即時測定を要求する検査ではない。
測定値を持たないprobeの空calibrationも引き続き許容する。物理ADCの精度・測定手順は未検査。

```sh
uv run pytest -q tests/test_virtual_bench_capture_resume_calibration.py
```

## 既存仮想captureの通知

`test_virtual_bench_capture_notifications.py`はraw eventの固定部分を独立に読み、Host decoderとも比較する。
logic/analogのone-shot完了、short stop、error、repeat満杯/解放、到達可能なtriggerで世代と
ポーリング結果を照合する。未購読なら送らず、満杯で予約stopped reason 2を送らない。
新start後までqueueに残る旧通知は元の世代を保ち、Hostが現在の世代以外を捨てることも確認する。
世代/時刻/停止理由の改変、通知欠落と不要なtriggerを故障注入して検出する。
stoppedのerrorはreason 3のときだけ照合し、それ以外で0を要求しない。
これは既存fixtureの通知動作の追加回帰。到達しないtrigger、全通知/streaming/並行性と
現在のcoreへ移行した全analog操作の適合は継続して整備する。

```sh
uv run pytest -q tests/test_virtual_bench_capture_notifications.py
```

## 到達しないトリガーとforce

`test_virtual_bench_capture_waiting.py`はunsigned ADCへのcross_up threshold 0を使い、
条件が成立しないときstate 2を保ち、通知/data/区画を作らないことを確認する。
再start/configureの拒否時不変、stop、error 1/2/3、releaseの非適用も検査する。
forceでは現在のsampleを使い、まだ足りないpretrigger分だけ区画を短くする。
groupでも全trackが待ち、forceが同じ時刻で始め、組のtrigger_fnは0を返す。

既存one-shot fixtureと同じく、成立後の将来sampleは即時に合成する。
これは物理的な待ち時間の証明ではない。自然な早期trigger、全modeのtriggerと
全group状態機械は別途整備する。

```sh
uv run pytest -q tests/test_virtual_bench_capture_waiting.py
```

## 疎なanalog roleの既存仮想ベンチ

`test_virtual_bench_analog_roles.py`はrole 1/3、0/3、3だけのplanについて、
configure/query、frontend/triggerの割当確認、raw dataとHost解析を検査する。
queryは設定済みdataを変えず、未割当roleへの拒否もcapture/plan/GPIOを変えない。
role 1と3を区別する既知sample列を使う。全analog/group契約の証明ではない。

```sh
uv run pytest -q tests/test_conformance_analog_payload.py tests/test_virtual_bench_analog_roles.py
```

## 最初の自然triggerとpretrigger不足

`test_virtual_bench_capture_pretrigger.py`は、pretriggerを待ち時間として使わず最初の条件成立で取得することを確認する。
不足した履歴はshort区画として返し、十分ある履歴は要求数だけ残す。
trigger_indexは残した区画内の位置であり、取得開始からのsample番号ではない。
logicのedge/level、analogのcross_up/down（疎なroleも含む）、pretrigger 0と不足/充足、
rawデータ・時刻・世代付き通知、groupのactual_rateに応じたP_kを照合する。
開始前のsampleをedge/crossingの比較に使わない。
one-shotは最後のsample時刻を待って区画を公開する。取得途中は区画を返さない。
[取得時間の検査](capture-metadata-conformance.ja.md#仮想captureの時間検査)を併用する。物理latency、全mode/全group契約は別途整備する。

```sh
uv run pytest -q tests/test_virtual_bench_capture_pretrigger.py
```

## 取得中・結合中も非適用のquery

`test_virtual_bench_capture_query.py`はplanのあるstate 0〜6と、group-boundの各状態を照会する。
未openの別Hostからqueryし、現在設定/data/区画/世代/plan/GPIO/bind/通知queueを変えないことを確認する。
状態を比較する検査は時計を固定する。別検査では時刻を進め、元の設定で取得が進む一方、
queryの候補rate/samples/modeは適用されないことも確認する。plan無しはcause 6、
取得中の不正rate 0はmalformedで、現在の取得を変えない。
configureの取得中/結合中拒否は継続し、queryと同じ扱いにはしない。
既存endpointのlegacy coreの検査であり、独立core適合fixtureとの同一視や全query契約の証明はしない。

```sh
uv run pytest -q tests/test_virtual_bench_capture_query.py
```

## repeat/streamingの開始trigger

`test_virtual_bench_capture_trigger_modes.py`は両modeのlogic/analogを確認する。
成立しない開始条件ではstate 2を保ち、data/通知/区画を作らない。
forceと既知波形の初回triggerで履歴を残し、repeatの最初の区画だけtrigger_index/不足時shortを付ける。
途中stopでもそのindexを残す。streamingはtriggeredとdataを送り、segment通知は送らない。
streamingの通し番号とerror時write_posは、捨てた開始前履歴を数えず実際の完了区画から計算する。

既知波形も経過済みsampleだけで条件成立を検出する。待機後の信号変化は下記の別試験で確認する。
物理latency、offset/skewを含む全modeの時間因果性、multirateの全幾何・storage gap・全stream区画契約は別途整備する。

```sh
uv run pytest -q tests/test_virtual_bench_capture_trigger_modes.py
```

## repeat/streamingのグループtrigger

`test_virtual_bench_capture_group_modes.py`はlogic/analogのどちらもtrigger元にし、
他方を異なるactual_rateで結合する。成立しない条件では全trackが待機し、dataを作らない。
forceでは共通の時刻で全trackを開始し、groupのtrigger_fnは0。自然triggerでは元のfnを返す。
各trackのP_k、履歴不足、最初のrepeat区画のtrigger_index/shortを確認する。
groupとtrackのtrigger通知は一度だけ送り、streamingではsegment通知を送らない。

これは経過済みsampleで条件を検出するtimestamp付き波形fixtureの回帰である。
待機中の信号変化は下記の別試験で確認する。実時間latency、
全group操作・multirate・storage gap・並行性の独立適合検査は別途必要。

```sh
uv run pytest -q tests/test_virtual_bench_capture_group_modes.py
```

## 待機後に変化するtrigger信号

`test_virtual_bench_capture_changing_trigger.py`は全3mode、logic/analogの単独取得とgroupを検査する。
開始時に成立しない信号を、sample番号を時刻に対応させた波形へ差し替えるfixtureを使う。
波形は過去のsampleを変えず、source sample 20から変化する。
待機中は経過した未観測sampleだけを確認し、edge/crossingの直前値を保持する。
同じ時刻で再照会しても再走査せず、再startでは観測位置をリセットする。
trigger通知と取得rawは同じ`VirtualCapture.value(role, sample)`の信号源を使う。

groupは全trackを進める前に条件を確認し、同じ成立時刻を各actual_rateへ写す。
status、各trackの操作、push、group force/stopの前にも進行を反映し、世代は進めない。
one-shotのgroup complete通知を一度だけ送ることも確認する。
このfixtureはtimestamp付きの入力関数であり、実機の非同期GPIO/ADC入力を証明しない。
start時の未来トリガー探索とone-shotの将来data合成は廃止した。
自然trigger/forceの成立時と取得完了時を分けて通知を照合する。
実時間latency、全multirate/storage gap・並行性・全group操作の独立適合は別途必要。

```sh
uv run pytest -q tests/test_virtual_bench_capture_changing_trigger.py
```


## channel時刻とgroupの写像

analogの自然triggerは、該当roleのskewを含む観測時刻を待ち、その時刻をtrigger_nsで返す。
groupはsourceの通知時刻を保持し、全trackをその時刻へ写す。sourceの最寄りbase sampleも
conditionを検出したchannelのframeと一致するとは限らない（4 channelの最後のchannelなど）。
forceのtrigger_nsは実行時刻で、trackのoffsetやsampling周期へ丸めない。
[取得時間の検査](capture-metadata-conformance.ja.md#仮想captureの時間検査)に98件のscopeを記載する。
取得区画のデータ・世代・plan roleの判定はこれらの時刻判定と併用する。
