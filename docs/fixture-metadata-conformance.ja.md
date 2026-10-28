# fixture系のdescribe metadata検査

`FixtureMetadataChecks(checks, selection).run()`は、呼び出し元が明示したGPIO/UART/I2C/SPIのfn/nameを第三段階で検査する。
revision 1に限定し、登録op検査も実施する。全要求はSID 0の読出しで、未選択の標準名を自動探索して操作しない。

- GPIO: 必須modes u32と入力mode 0。任意drive_levelsはn≥2、default<n、正確な要素数、mAの狭義昇順。
  選択されたGPIO fn間でdrive_levelsの存在と値が同じことも確認する。未選択fnとの一致は未検査。
- UART: formatsの要素数と固定形、8N1の存在、定義されたデータ長/パリティ/ストップbit。
- I2C/SPI: max_length u16、max_clock_hz u32、features u32、queue_depth u8を確認する。
  max_lengthを使う要求/応答が全経路のframeに収まることを検査する。
- I2C stretchを提供する場合、max_stretch_usは必須で正のu32。
- SPI cs_setup_nsは省略/0も許され、存在時だけu32を検査する。

I2Cの最大data要求は12＋max_length byte、応答は8＋max_length byte。
SPIは要求14＋max_length byte、応答12＋max_length byte。
交渉済みframeと比較し、全経路保証には64 byte最小frame、または唯一の広告経路のconfirmを用いる。
複数経路で最小frameを超える場合、他経路の上限を未確認のままPASSにしない。
仕様にないqueue_depth/max_length/max_clock_hzの正の下限や、drive mAの正の下限は課さない。

独立`FixtureMetadataModel`は17 interfaceの宣言だけを持つfixture。
選択したfnの第三段階は、前提、登録op、metadata、GPIO間一致を確かめる。
共通宣言と登録opは別にも確認する。
広告したopの動作やpin割当は実装しておらず、完全適合probeとして使わない。
configure/set/転送/plan寿命/電気的適合は未検査で、`full_conformance`はfalse。
公開側は私的ベンチや設備設定へ依存しない。

```sh
uv run pytest -q tests/test_conformance_fixture_metadata.py
```

Arduinoのuv/pytest環境からも公開コードのpathを明示する。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_fixture_metadata.py
```
