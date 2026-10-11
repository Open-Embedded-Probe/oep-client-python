# Host待受けループの期限開始点

SPEC checkoutを明示し、`HostDeadlineChecks(SampleHostDeadlineAdapter(), spec=path).run()`を実行する。
core §4.4/transports §6の本文SHAと待ちの数値を照合し、OEP固有のconfig仕様には依存しない。
private設備を探索しない。既存SerialLink._send/_exchange_once/_recv_forとLengthFrames.recvを、
予定したbyte列を返すScheduledStreamと仮想時計で通す。OSの口や実時計のsleepを使わない。

fn1/op16、引数wait_ms(u32)、空の成功payloadをこのfixtureの拡張として使う。
セッション7、max_frame64、inflight2/window256の事前に確定した経路を仮定する。
時間の意味はこのfixtureで定義し、OEP標準interfaceの操作を追加するものではない。
待ち予算はwait_ms＋1秒＋転送時間。要求message14byte、length frame16byte、COBS frame19byte。
lengthは転送時間0、UARTは(19＋64×3)×10/115200秒。

5項目、24条件を確認する。

- SINGLE12：length/COBS、引数0/1500/5000ms、write400/4000ms。返答は必要な待ちの1ms前に完成し、Hostがその完成まで受けられることを確認する。
- TIMEOUT4：length/COBS、引数0/5000ms、write4000ms、応答なし。書き終わりから少なくとも必要な時間を待つ。
- PIPELINE4：length/COBS、引数0/1500ms、write4000ms。2つをまとめて送り、2つ目の待ちを1つ目の完全な応答受領から始める。
- PIPELINE-TIMEOUT4：同じpipelineで2つ目だけ応答なし。1つ目の完全な応答受領から少なくとも必要な時間を待つ。

各返答は先頭2byteと残りを10ms離して到着させる。
2つ目の期限を最初のbyteの到着やbatchの書き終わりから数える故障を検出する。
時計はnsで制御し、検査する実装moduleのtime参照だけを一時的に差し替え、必ず戻す。
timeout後の_recoverは観測用hookで止める。再送/resyncの検査へ結果を広げない。
復帰時刻の完全一致はこのゼロ処理時間fixtureの条件で、実CPUに正確な応答時刻を要求しない。
応答なしの場合は下限以上を要求し、余分に待つことを禁止しない。

単体27件：正常1、実待受け処理の故障8、観測故障11、制御不足2、本文guard2、registry guard3。
短いtimeout、引数/余裕/転送の省略、書込み開始起点、pipelineの書込み/最初のbyte起点、
早い結果/timeout、異なるcorr、再書込み、誤ったfloor/長さ、実際の要求/応答byteを検出する。
既存実装の待受け処理は変更せずPASSした。
実時計の精度、物理transport、resync/retry/own-link交渉、pipeline内の異なる引数時間、
全production Host統合とregistry移行は残る。full_conformance=false。
