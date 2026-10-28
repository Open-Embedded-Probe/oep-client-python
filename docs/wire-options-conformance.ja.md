# wireのattachオプションの検査

`WireOptionsChecks`は選択SPECの`oep-if-debug.ja.md` §1/2/3/5とcore §2.3/8.1を
第三段階`oep-interface`で検査する。`Checks(send, registry, unit)`と
`SampleWireOptionsAdapter(source)`を明示して`WireOptionsChecks(checks, adapter).run()`を呼ぶ。
全4 fnの共通宣言は第二段階の`InterfaceChecks`へ分ける。
公開コードは設備や私的ベンチを探索しない。

明示論理fixtureはRVSWD/SWIO/SWD/consoleを持つ。
reset role 3はchannel 6、max_op_ms=5、最も遅い読出しは1000 Hzを宣言する。
この番号・時間・速さを他probeや設備へ要求しない。

- RVSWD idle_clockは省略/0でclock high、1でlow。dataはhigh。
  SWIOのdataはhigh、SWDはdata low / clock high。
  未定義idle値は受け取ったraw tagでunsupported。SWIO/SWDではidle_clockは未知のtagで、
  criticalならunsupported 0x84、noncriticalなら無視してattachする。
- reset channelは毎回hostが指定する。role 3未提供、未宣言channel、hold_ms超過はunsupported。
  既存connection、自分のwire pins、外部予約との競合はcause 1、disable/出力idleはcause 5。
  max_speed 0、壊れた幅なども含め、reset前に全要求を検査する（固定部分はdebug §1）。
- resetはopen-drainでlowに引き、指定hold後に駆動をやめてcoreの空き状態へ戻す。
  channelをconnectionへ持ち越さない。reset TLVが無ければ既定線を動かさない。
- resetを解いてから最も遅い速さでtargetが応えるまで待つ。
  RVSWD/SWIOはDMSTATUS、SWDはDPの応答を論理的に扱う。
  fixtureは失敗読出しごとに1 ms進め、2回line後に復帰する例と全体上限で諦める例を持つ。
  復帰後のattach flags/dpcを確認し、失敗はcompleted line。
  新しいconnection番号を割り当てず、試したピンを放し、既存connection/streamと駆動状態を保つ。
- RISC-V method 1はhalted flagとdpcを返す。reset解除とhaltまでに走りうるため、
  最初の命令前での停止保証にはしない。SWDのdormant wake flagはRISC-Vのflagsと区別する。
- SWD targetselはcriticalで明示する。connectionsのtidはattachで読んだDPIDR（scheme 2、4 byte）で、
  hostが与えたtargetselは応答TLV 0x01 selected_target（entry_index、targetsel）で別に返す（debug §2.1）。無指定ならTLVを付けない。
  同じpinsはtargetselが同じ/違う/無い場合いずれも新規attachを拒否する。
  別targetを選ぶには先にdetachし、新しい資源番号でattachする。
- 既知TLVのcritical bit有無で値を変えず、重複は最初を使う。後のコピーも検証し、未定義値はunsupported。
  attachの同一要求再送でreset/資源割当を繰り返さない。
- detachとsession endはstream/connectionを解放し、idle_clockの駆動もやめる。
  停止中のhartを勝手に再開/リセットしない。

`WireOptionsModel`は独立論理fixtureであり、電気的な駆動・waveform・target通信をしない。
`wire_behavior(ok|dm-recover|dm-line|dormant)`、`wire_reset_support(bool)`、
`wire_pin(unowned-channel, free|disabled|output|reserved)`は明示的な刺激。
予約状態はplan allocatorの代わりの論理刺激であり、plan統合の適合証拠にはしない。
各caseを新bootとし、raw交換、pin mode/level、reset/read時刻と資源/target metadataを保存する。

単体38件は22種類の誤動作注入、前提不足、不正刺激、所有pin変更拒否、共通宣言を含む。
`full_conformance`はfalse。scanのidle_clock/targetsel、物理open-drain/level/hold/wake/DM timing、
attachの速さ/scratchのアルゴリズム、線切れの応答/解放順、非RVSWDのidle=0の方針、
plan統合/別容量、実応答writerとproduction Host統合は未検査。
