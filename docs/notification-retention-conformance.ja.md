# 通知seqの保持範囲

SPEC checkoutを明示し、`NotificationRetentionChecks(SampleNotificationRetentionAdapter(), spec=path).run()`を実行する。
core/transports本文SHAとregistry待ち値を照合する。private設備や実transportを使わない。
core §2.6の「同時に持つ周回値の差が2^(w−2)未満」を、u16通知seqの明示サンプルで検査する。
サンプルの次seqカウンタも同じ保持空間に含め、カウンタと残るframeの差を16384未満に保つ。
これは保守的なサンプルの保持方針で、未保持のすべての過去通知を保持範囲へ含める規則ではない。
本文の固定は[待受け期限の検査](host-deadline-conformance.ja.md)と共有し、oep-spec a09fa45でcore §2.6/10.3/10.4、transports §3を再確認した。
a09fa45はcore §10.4に「送り始めたframeへ別の応答や通知を挟まない」「入らない通知は送り始める前に捨てる」
「閉じた経路の送信残りは捨てる」を明記した。いずれも既存のactive非差替え・未送信破棄・close時破棄の検査と一致し、検査の変更は不要。

4条件：開始seq0/65530、未送信のpending/先頭1byteだけ送信したactiveの組合せ。
session7/lease60000、仮想時計0、max_frame64、2fn/1経路。fn1で16386通知を実生成し、fn2は1通知のまま。
eventとdataを交互に生成し、破棄しても共有seqが進むことを確認する。
1、16383、16384、16386生成時点のカウンタ・保持frame全byte・active offset・byte量・経路状態を観測する。

境界未満では最古のframeを保持する。16384差になるところで未送信frameを捨て、fn2の別空間を保持する。
active frameは途中で別frameへ差し替えず、経路を閉じて破棄する。閉じてもCore session/購読は残る。
再び生成された通知は閉じた経路へ積まず、seqは進む。このclose/evictionはサンプルが採用する実現方法である。
規範が指定するのは保持範囲と非interleaveで、別の適合Probeの具体的なbuffer管理を固定しない。
`out`は送信済みのHost受領mailboxとして扱い、Probeの未送信保持領域はpushとactiveである。

初期検査で4条件とも保持範囲違反を検出した。既存RouteModelはbyte量だけを制限し、古いseqを無期限に残していた。
未送信frameの期限判定と、activeが境界へ達した場合の論理closeを追加した。Core/productionの変更はない。
単体21件：正常1、観測故障14、実model故障4、制御不足2。関連通知/経路を含む50件が通過する。
capture/dmseq等の他の周回空間、Hostの保持値、同時producer、OS/実writer bufferは別途検査する。
full_conformance=false。
