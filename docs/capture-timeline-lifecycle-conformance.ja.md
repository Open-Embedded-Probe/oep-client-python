# 異レートgroupの停止・再送・寿命検査

`CaptureTimelineLifecycleChecks`は、明示したSPEC（a09fa45）のcapture §2.1/3.2/3.4/4.1とcore §6/8/9/10に基づく第三段階の検査。
[取得timeline](capture-timeline-conformance.ja.md)と同じ独立model/adapterと既知の波形を使い、
ADCまたはlogicを1000Hzのfast、もう一方を333Hzのslowとする（one-shotは16 sample）。
私的ベンチ、endpoint、Hostのcapture decoderは必要ない。

fast 2種類それぞれで20ケース、計40構成を検査する。

- stopは両方が取得中（9ms）と、fastだけ完了した状態（20ms）で行う。
  到来したsampleの部分区画・short・開始時刻・世代・serial_done・write_posと生データを照合する。
  完了済みtrackへのstopは状態表のとおり何もせず成功する。
- fastまたはslowの取得失敗では、失敗した部分区画を公開せず、相手を止める。
  error値、失敗trackのread（write_pos 0）とpeerの短い区画を確認する。peerが既に完了していればstate 4とデータ・区画を保持する。
- startの再送は取得中、stopの再送は部分区画を完成させた後で行う。
  同じ要求の応答と状態が一致し、世代やデータを変えないことを確認する。
- queryは取得中と片方が完了した状態で両trackへ送る。
  提案した537Hz/5 sampleを答え、元の設定・timeline・データを保持することを確認する。
- endとforceによるsession交代は取得中、lease期限は取得中と取得完了後の保持データを持つ状態で確認する。
  bind、plan、購読、取得データと区画を解放し、pinをidleへ戻す。世代は保持する。
- 世代の使い切り: groupの世代が0xFFFFFFFFならgroup startを、trackの世代が0xFFFFFFFFなら
  group startとそのtrack単独のstartを、副作用前にunavailable cause 2で断る。世代は1へ戻らず、状態も変えない。
  もう一方の世代は独立に進む。
- startの初期化失敗: 先頭または2番目のtrackの開始に失敗させ、group startがcompleted failedで
  成功時と同じ固定形（start_ns、進めた世代、bind順のfn/世代）を返すことを確認する。
  groupと失敗trackはstate 6（error付き）、もう一方はstate 1。世代は戻さず、データを公開しない。
  同じ要求の再送は同じ応答を返し、次のstartは成功する。単独trackのstart失敗もcompleted failedでgeneration(u32)を返す。
- 状態表: bindしない1 trackで、plan無しのconfigure/query、state 0/1/3/4/6のconfigure・start・stop・releaseを確認する。
  releaseはone-shotでも先に世代を検証し、合えば何もしない。state 3のstopは短い区画を出し、state 6のstopは1へ戻す。
- streamingのエラー: 取得中の区画の一部を通知で送った後に失敗させる。
  write_posは最後に公開した区画の終わりで、受け取ったwrite_pos以降のbyteは捨てる対象になる。
  失敗した区画をsegmentsに載せず、stoppedはreason 3とerrorを持つ。peerは止まり、reason 1で終わる。

停止やエラー後に時計を進めてもsample取得が続かないことを確認する。
その後のstartは世代を進め、以前のデータを消し、旧世代のreadをcause 6で断る。再開したone-shotの区画時刻と生データも照合する。

lease検査は、次のwire要求で後始末されることだけを確認するものではない。
開始時に取得した1000ms leaseについて、999msでは所有し、1000msでは解放済みであることを
adapterで観測してからlock_stateを読む。session不要のstatus/readはleaseを延長しない。
このmodelは期限をsample到来と同じスケジューラへ入れ、同時刻なら期限切れを先に処理する。
これはfixtureの論理時計での制御であり、物理時計の精度を保証するものではない。

単体123件: 正常40、wire故障40、意味的故障39、再送副作用2、前提不足2。
意味的故障は、一部trackの停止漏れ、short欠落、完了区画消去、世代/write_posの誤り、queryの変更、
終了時のcache残留・世代初期化、再start時の古いデータ・時刻基準、期限遅延、エラー時のpeer停止漏れ・失敗区画公開・
error値・write_pos・完了peerの消去/state変更、世代の一周・使い切り時の副作用・cause誤り、
start失敗の成功応答・短い固定形・state・peerの取得継続・世代の巻き戻し、
状態表（取得中のconfigure受付、完了データの消去、世代を見ないrelease、state 6からのstart拒否、
state 6のstop、plan無しのquery、state 0のstopの副作用）、streamingエラーのwrite_pos・失敗区画の公開・reason・peerの継続を検出する。
全40構成をまとめて報告する。

`full_conformance`はfalse。既知の2 track/即時開始について順次刺激した検査である。
repeat/release、trigger待ちとforce（state 2/5）、他の構成・ADC layout/skew、保持境界、
同時要求やエラーの競合、物理時刻、production Hostは別途検査する。

```sh
uv run pytest -q tests/test_conformance_capture_timeline_lifecycle.py tests/test_conformance_capture_timeline.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_timeline_lifecycle.py
```
