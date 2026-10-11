# アナログ成功payloadと生データの検査

`AnalogPayloadChecks(checks)`は選択SPECのcapture §1.2/3.2/3.3/3.8を第三段階で確認する。
callerはconfigure/queryの成功payload、calibrationの成功payload、取得データと既知の生サンプル、
planのroles、describeのfrontend番号、modeとcore max_op_msを明示する。
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
