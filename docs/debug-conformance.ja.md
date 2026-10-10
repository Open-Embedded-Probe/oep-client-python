# 必須riscv-dm操作とconsole調停の論理検査

`DebugChecks`は選択SPECの`oep-if-debug.ja.md` §4/4.1/4.2と
`oep-if-console.ja.md` §3を第三段階`oep-interface`で検査する。
`Checks(send, registry, unit)`と`SampleDebugAdapter(source)`を明示して
`DebugChecks(checks, adapter).run()`を呼ぶ。設備や私的ベンチは公開コードから探索しない。

前提1項目＋動作21項目。明示fixtureのfn 5は標準riscv-dmの必須dmi/halt/resumeを提供する。
全5 fnの共通宣言は`InterfaceChecks`で別に検査し、第二段階の結果と分ける。

- dmiのn=0、write/read/waitの混在、異なる値の順序、done/status/nvalsと途中失敗のsuffix非実行。
- poll_reads/poll_usの一致・timeoutと0上限の1回読出し。poll timeoutは最後の値を足し、
  線の不良で読めなかった場合は値を足さない。statusはregistryのtimeout=4等と照合する。
- 未知kind、切れた手順、時間引数の和の超過を要求全体の実行前に拒否する。
  critical TLV・不正TLVの構造、資源の種類/系統、session/header優先順を確認する。
- raw dmiでhostがDATA0/DATA1へ書いた値をprobeが自動復元しない。再送はtargetアクセスを繰り返さない。
- 同じconnectionのconsole pollをdmiの各手順の間へ注入しても、DATA0/DATA1に触れない。
  応答が返った後はDMSTATUSを読んでからmailboxへ戻る。
- halt中のmailbox停止、resume後の復帰、resumereqを1回だけ出すこと。
  halt timeoutはhaltreqを下ろし、resume失敗はstatus state / completed failedで返す。

独立`DebugModel`は4つのwire/console fnへriscv-dmを追加する論理fixtureである。
max_op_msは1、DMIアクセスの論理コストは10 us。poll_readsで要求全体の上限へ達した場合も検査する。
実時計のDMI性能・待ち時間適合を証明する値ではない。
`debug_failure(index-or-None, status)`、`debug_hart_failure(bool)`、
`debug_registers(connection, u8-to-u32-dict)`で刺激し、
`debug_state()`で資源・アクセス・console pollの調停・buffer/pendingを観測する。
各caseは新bootでlogical restartする。targetや実機を操作しない。

単体35件には18種類の誤動作注入、前提不足、不正刺激、全5 fnの共通宣言検査を含む。
`full_conformance`はfalse。optional reset/step/run/block操作と状態復元、hartsel、
物理DMI/timeout/線の再試行、別connectionのschedule、実応答writerの順序、production Host統合は未検査。
同じconnection内の論理調停が通っただけで、全debug操作や実機適合とは扱わない。

応答長の境界検査を追加した（合計22項目）。値配列だけでもmax_frameを超える要求は、
先頭に書き込みがあってもtargetへ触れる前にunsupported/固定部分0で拒否する。
境界内ではすべての値を返す。省略可能な補助TLVを付ける余裕が無ければ省く。
単体では64 byte経路のDebug/Block/Step/Reset/Run/Retry/Cutモデルを確認し、
過大応答、拒否前書き込み、補助TLVによる境界超過も誤動作注入で検出する。
