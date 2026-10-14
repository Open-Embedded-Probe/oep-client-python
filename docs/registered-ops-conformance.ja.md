# 登録済みOEPインターフェイスのop宣言検査

`RegisteredOpsChecks(checks, selection).run()`は第三段階で、明示したfn/nameの必須opを検査する。
`selection`は呼び出し元が渡す辞書。標準名を自動探索して機能を実行せず、要求はすべてSID 0の発見・宣言読出し。
registryのop codeを使い、必須/任意の区別は各インターフェイスの規範文から定める。
lock有無や番号の空きから推測しない。revision 1の17 interfaceに対応する。

`RegisteredOpsModel`は独立した宣言だけのfixtureであり、広告したopの実装を持たない。
適合probeとして使わない。最小の必須opだけを宣言し、単体では宣言を壊して検査器を確認する。
前提1＋fn別17の18項目。全17 fnの共通宣言137項目は第二段階で別に実行する。
既存のwire/console宣言検査は、固有metadataを扱う追加検査として保持する。

- plan/restart、ARM-ADI、GPIO、SPIの表のopは必須。
- console/UARTのmarks/clear/mark/writeは任意。markを持つならmarksも持つ。
- wireのscan、I2Cのstretchは任意。linkのport_speedとport_stateは任意（port_speedにport_stateが伴うことはport_speed検査の追従で加える）。
- RISC-Vのhalt/resume/reset/block/run/stepは任意。read_block/write_blockは組で持つ。
- captureのforce/query/subscribe/unsubscribeは任意。capture-groupはforce/subscribe/unsubscribeが任意。
  subscribe/unsubscribeは組で持つ。analogのcalibrationは必須、logicには宣言しない。
- configのsave/eraseは任意だが組で持つ。saveを持つならstateも持つ。get/set/unsetは必須。
- 選択registryへ追加されたopを、既知の任意op名でなければ必須として確認し、黙って無視しない。
  optional政策とregistryが合わなければ前提で停止する。新標準名/revisionへの自動適用はしない。

任意opを持たないことは失敗にしない。必須のop bitmapを確認しただけで、op実動作や
必須describe tag、通知、resource lifetime、物理動作を検査済みとはしない。
`full_conformance`はfalse。公開コードは私的ベンチ・設備設定へ依存しない。

```sh
uv run pytest -q tests/test_conformance_registered_ops.py
```

Arduinoのuv/pytest環境からは次を使う。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_registered_ops.py
```
