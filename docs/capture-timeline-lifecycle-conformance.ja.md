# 異レートgroupの停止・再送・寿命検査

`CaptureTimelineLifecycleChecks`は、明示したSPECのcapture §3.2/4.1とcore §6/8/9に基づく第三段階の検査。
[取得timeline](capture-timeline-conformance.ja.md)と同じ独立model/adapterを使い、
ADCまたはlogicを1000Hzのsource、もう一方を333Hzのpeerとする。
one-shotの16 sample、sourceのP=2、自然trigger index3という既知の波形を使う。
私的ベンチやHostのcapture decoderは必要ない。

source2種類それぞれで17ケース、計34構成を検査する。

- stopはトリガ待ち、取得中、sourceだけ完了した状態で行う。
  到来したsampleの部分区画・short・開始時刻・世代・serial_done・write_posと生データを照合する。
  完了済みtrackへのstopは状態表のとおり何もせず成功する。
- sourceまたはpeerの取得失敗では、失敗した部分区画を公開せず、相手を止める。
  error値とpeerの短い区画を確認する。peerが既に完了していればstate 4とデータ・区画を保持する。
- startの再送は待機中と取得中、stopの再送は部分区画を完成させた後で行う。
  同じ要求の応答と状態が一致し、世代やデータを変えないことを確認する。
- queryは待機中、取得中、片方が完了した状態で両trackへ送る。
  提案した537Hz/5 sampleを答え、元の設定・timeline・データを保持することを確認する。
- endは待機中と取得中、forceによるsession交代は取得中で確認する。
  lease期限はトリガ待ちと取得完了後の保持データを持つ状態で確認する。
  bind、plan、取得cache、データと区画を解放し、pinをidleへ戻す。世代は保持する。

停止やエラー後に時計を進めてもsample取得が続かないことを確認する。
その後のstartは世代を進め、以前のデータを消し、旧世代のreadをcause 6で断る。
再開したone-shotのtrigger時刻・区画時刻・生データも照合する。

lease検査は、次のwire要求で後始末されることだけを確認するものではない。
開始時に取得した1000ms leaseについて、999msでは所有し、1000msでは解放済みであることを
adapterで観測してからlock_stateを読む。session不要のstatus/readはleaseを延長しない。
このmodelは期限をsample到来と同じスケジューラへ入れ、同時刻なら期限切れを先に処理する。
これはfixtureの論理時計での制御であり、物理時計の精度を保証するものではない。

単体90件: 正常34、wire故障34、意味的故障17、再送副作用3、前提不足2。
一部trackの停止漏れ、short欠落、完了区画消去、世代/write_posの誤り、queryの変更、
cache残留、終了時の世代初期化、再start時の古いgrid、期限遅延、
エラー時のpeer停止漏れ・失敗区画公開・error値変更・write_pos誤り、再startの時刻基準ずれを検出する。
全34構成をまとめたreportは40項目（登録op5・前提1・取得寿命34）。

`full_conformance`はfalse。既知の2 track/one-shotについて順次刺激した検査である。
通知、repeat/streaming、他の構成・ADC layout/skew、保持境界、
同時要求やforceとerrorの競合、物理時刻、production Hostは別途検査する。
完了済みpeerのデータ消去・state 1への変更を故障として注入し、保持規則の違反を検出する。

```sh
uv run pytest -q tests/test_conformance_capture_timeline_lifecycle.py tests/test_conformance_capture_timeline.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_timeline_lifecycle.py
```
