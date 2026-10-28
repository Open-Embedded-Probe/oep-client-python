# 適合検査とSPECの作業版

検査が要求する契約と、選択したSPECの契約は揃っている必要がある。
protocol revisionが同じ1でも、凍結前の仕様変更へ検査が自動的に追従するわけではない。

現行の検査CLIは、レビュー済みcore本文のSHAを確認し、異なる本文では機器を開く前に停止する。
core本文のSHAは実行結果のSPEC情報にも記録する。現在レビュー済みの本文はSPEC 02c08c9 のcoreである（循環するcorr16、flagsの無いconfirmとlist entry、TLV番号0の拒否、設定の op）。
Checksを直接使う場合も、循環corrの宣言や新しいheader幅を持つregistryは旧検査として受け付けない。
手書きregistryによるunit testは、選んだ検査条件の確認であり、任意のSPECへの適合を証明しない。

このcoreでは、corr16は循環し、ownerの表示と再送記録は任意、list entryは6byte、confirmはflagsを持たない。
旧検査の「65535でend」「corr枯渇前のsession切替」「list entryは7byte」は削除または書き換えた。
ガードのSHAだけを新しい本文へ更新して通す変更も行わない。

追従はコア、全拡張の共通契約、標準インターフェース固有の順に進める。
最小構成と主要な成功・拒否・復旧を先に確認し、任意機能の不在は適用外として扱う。
宣言した必須操作の不在や誤動作を、設備不足としてskipしない。
新しい検査が対象の契約を確認できるようになってから、対応するガードと文書を一緒に更新する。

## SPEC の共有ベクトルの読み方

`tests/test_spec_vectors.py` は `tests/vectors/*.json`（`tests/vectors/SPEC_COMMIT` の写し）を、このクライアントと仮想ベンチに当てる。

- cobs / headers / confirm / discovery / sessions / refusals / ops / ops_encoding / core_extensions /
  interface_profiles / logic_layout: 全ケース。ops.json の events と data（capture の出来事とデータフレーム）も含む。
- interface_boundaries: batching / reception / serial_loss / storage / groups / fixtures の全ケース。
- checks.json: 全ケース。dmseq の CRC-8 と DATA0 の語は、dmseq の検査器の参照実装と仮想ベンチの dmseq モデルに当てる。
- core_lifecycle: 全シナリオ。仮想ベンチの経路ごとの受付（`Endpoint.receive` / `process` / `sent` /
  `close_route`）に、再送記録 0 件で当てる。
- core_policy: 全ケース。仮想ベンチの任意の受付方針（試験フック `Endpoint.policy`）に当てる。下流の応答は
  ベクトルの仮の値なので、方針で断られないことだけを比べる。
