# 選択した標準describe metadataの検査

[English](selected-metadata-conformance.md)

`SelectedMetadataChecks(checks, selection).run()`は、明示したfn/nameについて第三段階の検査を行う。
restart、riscv-dm、arm-adiのrevision 1を扱い、登録op検査も実施する。全要求はSID 0の読出し。

- restart: `restart_max_ms`は必須u32。仕様にない正の下限は課さない。
- RISC-V: blockを提供するときは`max_length`が必須u16で、byte単位の4の倍数。
- ARM-ADI: blockを提供するときは同じ上限条件を適用する。`features`はbitを立てない（debug §6はbitを定めない。0の値は許す）。

`max_length`はfnの上限で、経路の上限ではない。hostは経路ごとにmax_lengthとmax_frameから決まる長さの小さい方を使う（debug §4.5）ので、経路のframeとは照合しない。
`max_length=0`も、仕様に正の下限がないため拒否しない。

`SelectedMetadataModel`は標準インターフェースの宣言だけを持つ独立fixture。
全fnの共通宣言と登録opも別に確認する。
広告したopの実動作は持たず、適合probeとは扱わない。欠落・型・容量・禁止tagを壊す単体テストを含む。
他の必須metadata、restartの所要時間、bus動作、物理配送は検査しない。
`full_conformance`はfalse。私的ベンチや設備の暗黙検索には依存しない。

```sh
uv run pytest -q tests/test_conformance_selected_metadata.py
```

Arduinoのuv/pytest環境からも明示した公開コードを使える。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_selected_metadata.py
```
