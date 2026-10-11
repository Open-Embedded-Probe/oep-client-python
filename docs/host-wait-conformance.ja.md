# Hostのsave待ち時間と転送時間の部分検査

SPEC checkoutを明示し、`HostWaitChecks(SampleHostWaitAdapter(), spec=path).run()`を実行する。
core §4.4、transports §6、config §2の本文SHAと待ちの数値/現在のsave番号を照合する。
private設備を探索せず、MemoryStreamだけを使う。OSの口を開いたり実時計で待ったりしない。

既存Host.expecting、ProbeConfig.saveの継承した時間処理、SerialLink._write/_wait/_limitsを通す。
既存registryのSAVEは旧番号3のため、検査用CurrentSaveBudgetでSAVE=18を明示的に投影する。
productionの番号やregistryは変更せず、各観測で旧番号と投影番号を記録する。
この結果から既存consumer全体の現SPEC wire適合を導かない。production_opcode_alignment_proven=falseを維持する。

4項目、87観測。SAVE72、失敗6、confirm履歴9。
max_op_ms1/5000/600000、max_frame64/256/65535、外側の待ち0/7000ms、
length framingとUARTの現在baud1200/115200/3000000を組み合わせる。
短い設定timeout0.05秒、lease1秒の速度変更経路でも、saveのmax_op_ms＋1秒＋転送時間を下回らない。
UART転送時間は、実際に書いたフレームの長さLと最新max_frameを使い、(L＋max_frame×3)×10/baud。
COBS/CRCを含むsaveのLは15byte、length framingは12byteで転送時間0。
必要以上に長い待ちは許す。float計算の下限照合は1e-12秒の誤差を許すが、物理タイマーの許容誤差を定めない。

外側の長いHost.expectingを捨てず、save成功/失敗の後に元へ戻す。
confirmの応答が来る前はmax_frame64、その後は直近の応答値を使う。
256→64→65535の実Host.confirm履歴で、各要求の待ちに使う値は64→256→64となる。
現在のconfirmがまだ返していない新しい値や、過去の最大値で計算しない。

単体25件：正常1、budget/scope故障13、制御不足3、本文guard3、registry guard5。
引数時間/余裕/転送/通知の省略、message長とwire長の混同、leaseや短いtimeoutによる短縮、
外側待ちの消失/失敗時の漏れ、save投影番号/結果/latest confirmの不正を検出する。
時間処理は既存実装のままPASSし、production sourceは変更しない。
実際の期限開始/timeout発生/再送、pipelineの先行応答待ち、link自身の交渉待ち、
他opの引数時間、実transport/flash、旧registryからのwire移行と全Host統合は残る。full_conformance=false。
