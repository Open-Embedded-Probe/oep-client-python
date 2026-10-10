# target側dmseqの論理検査

`TargetDmseqChecks`は選択SPECの`target-console-dmseq.ja.md`のtarget規則と
timeoutを第三段階`oep-interface`で検査する。`Checks(send, registry, unit)`と
`SampleTargetDmseqAdapter(source)`を明示して`TargetDmseqChecks(checks, adapter).run()`を呼ぶ。
公開コードから設備・私的ベンチを探索しない。

前提1項目＋動作17項目。対象は短い待ち20 ms、長い待ち1000 ms、入力容量2 byte、
出力queue容量6 byteを明示した論理fixtureである。この容量や待ちの上限を一般実装へ要求しない。
仕様の実時間の下限を、この結果だけで満たしたとは報告しない。

- target payload 0〜6、DATA1→DATA0の書込み順、CRC・S/A/SYN/TOと初期S/Aの4組合せ。
- 自分と異なるbit 7=1 wordは即再送。不正M/CRC/Kは同じframeを再送し、受付やdeadlineを進めない。
- DATA0=0はackにしない。短い待ちごとに再送しつつ、timeoutまでの待ち時間を保持する。
- 短い待ち19/20、長い待ち999/1000 msの境界。timeoutは同じS/payloadにTOを付け、CRCを再計算する。
  出したまま20 msごとに再送し、timeout中の新しい出力を捨てる。
- 有効な遅い答えで通常動作へ戻り、次のframeでTOを下ろす。ack後は長い待ちに戻る。
- Hの重複入力を二度渡さず、同内容でも新Hなら受け取る。M=0ではAを更新しない。
- 入力が満杯/不足ならpayloadとAを受け付けず、空き回復後の同じHを受理する。
- 出力queueを先頭順に送る。独立probeモデルとの論理mailbox往復で、OEP write/read、
  target入力のbackpressure、pending保持と再送、重複抑止を確認する。

独立`TargetDmseqModel`の`Target`は明示的な論理時計を持つ。
`target_begin(s, a, payload)`、`target_host_word(u32)`、`target_service(advance_ms)`、
`target_write_output(bytes)`、`target_read_input(count)`で刺激する。
`target_link_poll(stream)`は生きたdmseq streamとtargetのwordを接続し、双方を1回進める。
target/probe双方のsnapshotとpollごとのDATA0/DATA1操作をadapter traceへ保存する。
CRCの期待値は検査側の多項式除算で計算し、モデルのbit逐次計算を参照しない。

単体30件に15種類の誤動作注入、前提不足、不正刺激の無変更確認を含む。
DATA1順序、早すぎるtimeout、0のack化、外来wordへの停止、不正Mの受付、入力重複/満杯時ack、
空ackのA更新、timeout時S反転、定期再送の欠落、timeout中の出力受付、SYN保持、出力逆順、
往復時の入力逆順を検出する。

`full_conformance`はfalse。実時間・割込み禁止中のclock、target firmware統合、
別容量/待ち方、OEP resync/TO/framing markの条件は未検査。外側のrunnerが実時計を使っても、
このtarget fixtureの待ち時間は論理時計の結果として分けて報告する。
