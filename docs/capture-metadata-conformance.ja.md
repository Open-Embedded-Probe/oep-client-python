# capture/configのdescribe検査

`CaptureMetadataChecks(checks, selection).run()`はlogic/analog/capture-group/configのfn/nameを明示して実行する。
revision 1の第三段階で、登録op検査を含む。全要求はSID 0読出し。

- capture: features u32の予約bit0〜2は0。上位bitはこの検査で禁止しない。
- track: modeの9 byte固定形とモードごとに1行、rate_rangeの9 byteとexactの0/1、channels u8、triggerの8 byte。
- analog: frontendの13 byte固定形と番号ごとに1行。
- capture-group: tracksの要素数とu16列の形。
- logic multirate: 存在時は13 byte、sample policy必須、2≤min_d≤max_d、pow2は0/1。
  壊れた広告を検査器はFAILとする。Hostが広告を使わず基本captureへ戻る動作の検査は別途必要。
- config: itemsの存在、slots_max u8、wifi_maxの存在時はwifi itemと正のu8容量。
  itemsがwifiを含む場合、confirmのmax_frameはregistryのwifi_min_max_frame（現行112）以上。
  全経路へ適用される条件なので、他経路の上限未確認はPASSにせず、唯一の広告経路をconfirm済みの場合に限る。

本文のstorage/wifi_maxは「持つときに限り」、registryコメントは「そのとき必ず」と読める。
この検査は本文に基づき、wifi itemからwifi_maxの存在を逆に必須とはしない。
storageの提供条件は既存SelectedMetadataChecksで別に確認する。
itemsの未知tagを独自拡張でないと推測して禁止せず、slots_maxの0も許す。

`CaptureMetadataModel`は17 interfaceの宣言だけを持つ独立fixture。
4 fnで10項目、全fn共通宣言137項目と登録op18項目は別に確認する。
広告opを実装せず、完全probeとして使わない。ここで検査した固定形を、容量/rate/frontendの実動作や
track bindの適合証拠としない。configure/取得/read/release/通知/世代/plan lifetimeは追加検査が必要。
`full_conformance`はfalse。公開側は私的ベンチへ依存しない。

```sh
uv run pytest -q tests/test_conformance_capture_metadata.py
```

Arduinoのuv/pytest環境からは公開clientのpathを明示する。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_metadata.py
```

## 仮想captureの時間検査

取得動作の回帰はdescribe検査とは別に実行する。

```sh
uv run pytest -q tests/test_virtual_bench_capture*.py tests/test_virtual_bench_analog_roles.py tests/test_capture.py
```

`test_virtual_bench_capture_initial_trigger_time.py`では注入した時計と波形で、logic/analog、
one-shot/repeat/streaming、単独/groupの12構成を確認する。start時に未来の波形を探索せず、
経過したsampleで条件を初めて観測するまではwaitingのままで、trigger/data通知を出さない。
検出後のtrigger時刻と世代も確認する。既知波形の試験でも、時計を明示的に進めてから検出を確認する。

queryはplanがあればstate 0〜6とgroup-boundでも使い、設定・取得データ・世代・bindを変えない。
この規則の回帰は`test_virtual_bench_capture_query.py`で確認する。

仮想one-shotは、トリガー後の区画（トリガー無しならstart後の区画）をまだ先に合成する。
上記12件を、取得完了・全データ生成の時間因果性や実機適合の証明にはしない。
