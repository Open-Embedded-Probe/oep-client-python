# 適合検査とSPECの作業版

検査が要求する契約と、選択したSPECの契約は揃っている必要がある。
protocol revisionが同じ1でも、凍結前の仕様変更へ検査が自動的に追従するわけではない。

現行の検査CLIは、レビュー済みcore本文のSHAを確認し、異なる本文では機器を開く前に停止する。
core本文のSHAは実行結果のSPEC情報にも記録する。現在レビュー済みの本文はSPEC a09fa45 のcoreである（循環するcorr16、flagsの無いconfirmとlist entry、TLV番号0の拒否）。
Checksを直接使う場合も、循環corrの宣言や新しいheader幅を持つregistryは旧検査として受け付けない。
手書きregistryによるunit testは、選んだ検査条件の確認であり、任意のSPECへの適合を証明しない。

a09fa45のcoreでは、corr16は循環し、ownerの表示と再送記録は任意、list entryは6byte、confirmはflagsを持たない。
旧検査の「65535でend」「corr枯渇前のsession切替」「list entryは7byte」は削除または書き換えた。
ガードのSHAだけを新しい本文へ更新して通す変更も行わない。

追従はコア、全拡張の共通契約、標準インターフェース固有の順に進める。
最小構成と主要な成功・拒否・復旧を先に確認し、任意機能の不在は適用外として扱う。
宣言した必須操作の不在や誤動作を、設備不足としてskipしない。
新しい検査が対象の契約を確認できるようになってから、対応するガードと文書を一緒に更新する。

## SPEC の共有ベクトルの読み方（a09fa45）

`tests/test_spec_vectors.py` は `tests/vectors/*.json`（a09fa45 の写し）を、このクライアントと仮想ベンチに当てる。

- cobs / headers / confirm / discovery / sessions / refusals / ops / ops_encoding / core_extensions /
  interface_profiles / logic_layout: 全ケース。ops.json の events と data（capture の出来事とデータフレーム）も含む。
- interface_boundaries: batching / reception / serial_loss / storage / groups / fixtures の全ケース。
- checks.json: COBS の CRC は全ケース。dmseq の CRC8 は端末側の枠の検査なので、このクライアントのものではない
  （dmseq の検査器 `conformance_dmseq` が扱う）。
- core_lifecycle（処理中の lease・経路の受付の契約モデル）と core_policy（任意の受付ハーネス。方針判定の
  unavailable cause 7 は任意機能で、仮想ベンチは持たない）は、SPEC 自身の試験が独立したモデルに当てるベクトルで、
  同期的に答える仮想ベンチにはそのまま当てない。
