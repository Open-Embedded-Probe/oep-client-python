# 適合検査とSPECの版

検査が要求する契約と、選択したSPECの契約は揃っている必要がある。
凍結前の仕様はprotocol revisionが同じ1でも変わるので、検査はレビューした本文に固定する。

各検査器は、レビューした本文のSHA-256をコードに持つ（`REVIEWED_CORE_SHA256`、`REVIEWED`など）。
異なる本文のSPEC checkoutでは、機器を開く前に止まる。core本文のSHAは実行結果のSPEC情報にも記録する。
`tools/sync_registry.sh`はSPECの本文のSHAを`tests/spec/DOCS_SHA256`へ書き、
`tests/test_conformance_reviewed_pins.py`は検査器の固定がそれと一致することを確かめる。
仕様を取り込むときは、検査の中身をレビューしてから固定を上げる。固定だけを上げて通すことはしない。
手書きregistryによるunit testは、選んだ検査条件の確認であり、任意のSPECへの適合を証明しない。

検査は、コア、全拡張の共通契約、標準インターフェース固有の順に積む。
最小構成と主要な成功・拒否・復旧を先に確認し、任意機能の不在は適用外として扱う。
宣言した必須操作の不在や誤動作を、設備不足としてskipしない。

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
