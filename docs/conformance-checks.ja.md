# SPEC からの適合検査

仕様を先に定め、適合テストを作り、コアを固めてから実装を追従させる。検査器は指定した SPEC checkout を読み、実装側の旧 registry から期待値を作らない。Python の Host/virtual probe/Arduino firmware は、この検査器を追加しただけでは新仕様に追従しない。

検査結果は次の3段階に分ける。

| 段階 | すべての実装への適用 | 検査する契約 |
|---|---|---|
| core | 全 OEP 端点 | transport、要求/応答、拒否順序、再送、session、時間、資源寿命 |
| interface | 独自拡張を含む全インターフェース | 名前/revision/instance、describe/ops、TLV、共通の資源・購読・ページング規則。各機能で適用されるものを検査 |
| oep-interface | 実装する標準 OEP インターフェース | 各 op の動作・副作用・競合・異常系。独自拡張向けテストの雛形として再利用 |

`oep-conformance` は最初の2段階の一部を実行する。PASS は実行した項目だけの成功で、全体適合ではない。JSON の `full_conformance` は false、`levels` と `unchecked` に範囲を残す。OEP固有の動作検査はまだ実行しない。

## 実行

`tests/equipment/.env.example` をコピーし、明示的に読み込む。設定は安定した port/名指しした USB/TCP、期待する unit ID、SPEC checkout、既存の共有 lock、新規 JSON 出力先。SPEC は隣接 checkout を自動探索しない。私的設備台帳への依存はない。配線・target の指定はこのコア検査には不要。

```sh
uv run --group dev --env-file tests/equipment/.env pytest tests/equipment/test_core_contract.py
# 同じ検査器のCLI入口
uv run --env-file tests/equipment/.env oep-conformance
```

出力先の親ディレクトリを先に用意する。既存の結果を上書きしない。共有 lock を接続から close まで保持し、自分の session を終了する。別利用者の session は force しない。pin 操作、設定変更、flash は行わない。現行仕様へ未追従の実装も FAIL として記録する。

## 現在の粒度と限界

| 項目 | 検査 |
|---|---|
| confirm | magic/revision、固定部、frame/window/inflight、transport、boot ID |
| discovery | 個体照合、list の件数/境界/重複、describe のページング/末尾/不変性、ops の形、transport対応、max_op_ms |
| headers/TLV | 結果 role/corr/resolution/detail/長さ、拒否 payload、corr 0、session 0、TLV ID 0 |
| session | keepalive、他 session の lock 拒否、lease 下限/上限、自然失効 |
| replay | 完全一致結果または result_lost、同 corr の要求変更拒否、同 ID open 後の履歴、end後の再送、open再送による復活禁止、再送によるlease延長禁止 |
| u16 | 半周を越えた corr、session 0 の独立性、corr 65535 の end。番号を一周させない |
| interface 共通 | 各 fn を独立に describe/ops/不変性/instance 検査。独自インターフェースにも同じ検査 |

各項目に条項、PASS/FAIL/BLOCKED、要求と応答 hex、経過時間を保存する。timeout/transport切断やcleanup失敗の後は回復を隠さず BLOCKED にし、実行全体は FAIL。設定が全くない任意の pytest 実機入口だけ SKIP。不完全な設定、アクセス権不足、個体違い、仕様違反は FAIL。

`result_lost` は仕様が許す保持サイズ上限を考慮して受ける。履歴の古い clock を送り直したとき、新しい時計値で completed を返せば再実行として失敗する。cacheの容量を実装固有の固定値と決めつけない。

transportの破損/分割/結合/再接続、複数経路、要求の重複応答、force時の資源解放、subscription、電気的なpin解放、複数target・各opの動作は未検査。現時点の transport は現行 client の framing を利用するため、独立した framing 検査でもない。これらを埋める前に全コア準拠・全インターフェース準拠と名乗らない。

既存 `oep-hardware preflight` は個体・旧仕様の宣言・open/end に限った別契約。portable C++、共有ベクタ、仮想テストの成功も、実機適合とは別に報告する。

既存の registry/vector は `tests/vectors/SPEC_COMMIT` と `SPEC_SHA256` で固定し、通常試験はその snapshot を検査する。明示した `OEP_SPEC_DIR` があれば、同じ commit の upstream 内容も比較する。SPEC の HEAD が先行しても旧回帰は自動同期しない。更新は `OEP_SPEC_DIR=/explicit/path OEP_SPEC_REF=<commit> tools/sync_registry.sh` で行う。
