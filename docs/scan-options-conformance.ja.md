# scanのidle_clockとtargetselの検査

`ScanOptionsChecks`は選択SPECの`oep-if-debug.ja.md` §1/3/5とcore §2.3/5/8.1を
第三段階`oep-interface`で検査する。`Checks(send, registry, unit)`と
`SampleScanOptionsAdapter(source)`を明示して`ScanOptionsChecks(checks, adapter).run()`を呼ぶ。
共通宣言は第二段階の`InterfaceChecks`へ分ける。公開コードは設備や私的ベンチを探索しない。

明示候補だけを有限個試し、connectionを作らない。
fixtureは前回のwire attachモデルにscanを追加し、max_op_ms=5、既定候補上限2、
最も遅い速さ1000 Hz、候補ごと1 msの論理時計を使う。他probeへこの値を要求しない。

- RVSWD idle_clockは省略/0ならclock high、1ならlow。dataはhigh。
  SWIOのdata high、SWDのdata low/clock highと区別する。
  読出し時の実際の論理levelを記録し、各候補のreserve→drive→read→releaseの順を確認する。
  TARGETSELを送る場合はread前にselectを1回入れる。
- SWDのscan targetselはtag 0x06で単一のu32を指定する。attachのtag 0x02と混同しない。
  指定したtargetだけを試す。無指定はTARGETSELを送らず、multidrop targetを自動選択しない。
  別selectorへのフォールバックや複数selectorの自動検索をしない。明示0も省略と区別する。
- DPIDRが読めればarm-adi entryを返す。RISC-VはDMSTATUS.version 2以上かつ15以外。
  応答のkind、ID、pins、順序とtriedを確認し、未試行候補を報告しない。
- max_speed無しは最も遅い速さ、有りならその値以下。最初の既知TLVを使い、
  critical bitの有無で意味を変えない。不正widthやcritical、未対応wireのオプションを拒否する。
- count=0/重複候補はmalformed。後ろに未宣言/使用中/disabled/output/reserved pinがあっても、
  先頭候補へ触れる前に全候補を検査する。所有pin/予約はcause 1、disabled/outputはcause 5。
  session優先順と不正TLVも検査する。
- triedは要求の先頭からの有限prefix。上限1のfixtureでは未試行prefixを次の要求へ明示して探索する。
  各候補の後でpinを空き状態へ戻し、既存connection/stream/target metadataやidle設定を変えない。
- 同一要求再送は保存結果を返し、再選択・再探索・pin操作を繰り返さない。

`ScanOptionsModel`は独立論理fixture。`scan_identity(fn, pair, selector, id-or-None)`で
読める識別子を明示し、`scan_limit(1..3)`で有限prefixを指定する。
selectorはSWDだけで使い、Noneは省略。配線/実targetを探索するものではない。
新bootごとにraw交換、選択、read時のlevels/速さ/ID、解放順と資源状態を保存する。

単体36件は18種類の誤動作注入、前提不足、不正刺激、共通宣言、
新fixtureでの既存attachオプションを含む。`full_conformance`はfalse。
物理wake/dmactive/idle cycles/時間、線切れの応答/解放順、他budget/frame経路とwriter並行性、
plan統合とproduction Host統合は未検査。
