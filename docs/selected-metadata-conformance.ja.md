# 選択した標準describe metadataの検査

`SelectedMetadataChecks(checks, selection).run()`は、明示したfn/nameについて第三段階の検査を行う。
restart、config、riscv-dm、arm-adiのrevision 1を扱い、登録op検査も実施する。全要求はSID 0の読出し。

- restart: `restart_max_ms`は必須u32。仕様にない正の下限は課さない。
- RISC-V: blockを提供するときは`max_length`が必須u16で、byte単位の4の倍数。
- ARM-ADI: 必須blockにも同じ上限条件を適用し、`features`の宣言を禁止する。
- config: `storage`を宣言した場合、save/eraseの提供と正のu32容量を確認する。
  save/eraseからstorageの存在を逆に必須とはしない。仕様文のonly-if条件に従う。

block上限では、要求18 byte＋data、応答8 byte＋dataが経路のframeに収まることを確認する。
全経路に保証される64 byte最小frameで収まる場合は、その下限を証拠にする。
それを超える場合は、広告された経路がconfirm済みの唯一の経路であることを要求する。
他経路の上限が不明な状態をPASSにしない。各経路の独立confirm収集は未対応。
`max_length=0`も、仕様に正の下限がないため拒否しない。

`SelectedMetadataModel`は17 interfaceの宣言だけを持つ独立fixture。
この検査の選択は4 fnで10項目、全fnの共通宣言137項目と登録op18項目も別に確認する。
広告したopの実動作は持たず、適合probeとは扱わない。欠落・型・容量・禁止tagを壊す単体テストを含む。
他の必須metadata、restartの所要時間、bus/storage動作、物理配送は検査していない。
`full_conformance`はfalse。私的ベンチや設備の暗黙検索には依存しない。

```sh
uv run pytest -q tests/test_conformance_selected_metadata.py
```

Arduinoのuv/pytest環境からも明示した公開コードを使える。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_selected_metadata.py
```
