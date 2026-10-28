# Host待受けループの期限開始点

SPEC checkoutを明示し、`HostDeadlineChecks(SampleHostDeadlineAdapter(), spec=path).run()`を実行する。
core §4.4/7.5、transports §3/6の本文SHA（oep-spec 02c08c9）と待ちの数値を照合し、OEP固有のconfig仕様には依存しない。
private設備を探索しない。既存SerialLink._send/_exchange_once/_recv_forとLengthFrames.recvを、
予定したbyte列を返すScheduledStreamと仮想時計で通す。OSの口や実時計のsleepを使わない。

fn1/op16、引数wait_ms(u32)、空の成功payloadをこのfixtureの拡張として使う。
セッション7、max_frame64、inflight2/window256の事前に確定した経路を仮定する。
時間の意味はこのfixtureで定義し、OEP標準interfaceの操作を追加するものではない。
fixtureのop16は要求全体の短い処理上限を定めない。

待ちの下限はcore §4.4に従い、「要求全体の処理上限＋1秒＋転送時間」。
処理上限はmax_op_msで、引数の待ち時間だけを上限に使わない（旧本文の「引数時間＋1秒」からの変更）。
fn 0のconfirm/list/describe/clock/keepalive/lock_stateはmin(max_op_ms, core_quick_op_max_ms=1000)、
max_op_msを取得する前は1000 ms（下限2秒と転送時間）。registryのcore_quick_op_max_msとmax_op_ms_maxも照合する。
op16要求message14byte、length frame16byte、COBS frame19byte。clock要求は10/12/15byte。
lengthは転送時間0、UARTは(wire長＋64×3)×10/115200秒。

production Hostは要求ごとに処理上限を選ぶ（Host.op_bound_ms、host-wait検査で確認）。
このサンプルアダプタは処理上限を明示的にSerialLink.expected_sへ与え、待受けループの起点と下限の扱いだけを検査する。
reportの`bound_source`にこの採用を記録する。


- SINGLE16：length/COBS、(引数, max_op_ms)＝(0,1)/(0,5000)/(1500,5000)/(5000,5000)、write400/4000ms。返答は必要な待ちの1ms前に完成し、Hostがその完成まで受けられることを確認する。引数がmax_op_msより短い条件で引数だけの下限を検出する。
- TIMEOUT4：length/COBS、(0,1)/(1500,5000)、write4000ms、応答なし。書き終わりから少なくとも必要な時間を待つ。
- PIPELINE4：length/COBS、(0,1)/(1500,5000)、write4000ms。2つをまとめて送り、2つ目の待ちを1つ目の完全な応答受領から始める。
- PIPELINE-TIMEOUT4：同じpipelineで2つ目だけ応答なし。1つ目の完全な応答受領から少なくとも必要な時間を待つ。
- QUICK6：length/COBS、fn 0 clock（session 0）、max_op_ms未取得/300/5000。未取得と5000では1000 ms、300では300 msを処理上限とする。

各返答は先頭2byteと残りを10ms離して到着させる。
2つ目の期限を最初のbyteの到着やbatchの書き終わりから数える故障を検出する。
時計はnsで制御し、検査する実装moduleのtime参照だけを一時的に差し替え、必ず戻す。
timeout後の_recoverは観測用hookで止める。再送/resyncの検査へ結果を広げない。
復帰時刻の完全一致はこのゼロ処理時間fixtureの条件で、実CPUに正確な応答時刻を要求しない。
応答なしの場合は下限以上を要求し、余分に待つことを禁止しない。

単体35件：正常1、実待受け処理の故障8、観測故障11、旧下限（引数時間だけ、fn 0の0 ms、max_op_ms未取得時の0 ms）5、
制御不足2、本文guard2、registry guard6。
短いtimeout、引数/余裕/転送の省略、書込み開始起点、pipelineの書込み/最初のbyte起点、
早い結果/timeout、異なるcorr、再書込み、誤ったfloor/長さ、実際の要求/応答byteを検出する。
既存実装の待受け処理は、処理上限を与えれば変更なしでPASSする。
実時計の精度、物理transport、resync/retry/own-link交渉、pipeline内の異なる処理上限、
操作が定める短い処理上限、production Hostによる処理上限の選択、
全production Host統合とregistry移行は残る。full_conformance=false。
