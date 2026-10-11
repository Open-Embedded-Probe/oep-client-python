# アナログ成功payloadと生データの検査

`AnalogPayloadChecks(checks)`は選択SPECのcapture §1.2/3.2/3.3/3.8を第三段階で確認する。
callerはconfigure/queryの成功payload、calibrationの成功payload、取得データと既知の生サンプル、
planのroles、describeのfrontend番号、modeとcore max_op_msを明示する。
layout.orderはplanのrole番号そのもの。飛び番も扱い、生サンプルの各行はrole番号の小さい順で渡す。
例えばroles=(1,3)でorder=[3,1]なら、各行の2番目の値が最初の枠に入る。
公開checkerは設備・私的ベンチ・隣接SPECを探索せず、Hostのcapture decoderも使わない。

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
logic/analog双方を確認する。state 5のqueryの断り方はこの検査で規範を追加しない。
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
