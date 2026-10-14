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
- wireのscan、I2Cのstretchは任意。linkのport_speedとport_stateは任意。port_speedを持つならport_stateも持つ。
- RISC-Vのhalt/resume/reset/block/run/stepは任意。read_block/write_blockは組で持つ。
- logic/analogのconfigure/start/stop/status/read/segmentsは必須。force/queryは任意（capture §3.2）。
  releaseはdescribeのmode（0x40）にmode 2を宣言したときだけ必須、ほかでは任意（capture §1/§3.2）。
  subscribeはmode 3を宣言したときだけ必須、ほかでは任意（capture §3.2/§3.4）。subscribe/unsubscribeは組で持つ。
  analogのcalibrationは任意、logicには宣言しない（analogのcalibrationの番号で確かめる）。
- capture-groupはbind/start/stop/statusが必須、subscribe/unsubscribeは任意の組。forceはv1に無く（capture §4.1/§4.3）、
  logicのforceと同じ番号0x13を宣言しない。
- modeの行は先頭byteだけを読む。modeの形・重複・個数は[capture/configのdescribe検査](capture-metadata-conformance.ja.md)が扱う。
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
