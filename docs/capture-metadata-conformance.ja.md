# capture/configのdescribe検査

`CaptureMetadataChecks(checks, selection).run()`はlogic/analog/capture-group/configのfn/nameを明示して実行する。
revision 1の第三段階で、登録op検査を含む。全要求はSID 0読出し。

analogの取得操作は[analog操作ガイド](analog-conformance.ja.md)の別checkerで確認する。
groupの結合・一斉取得・世代と寿命は[capture-group操作ガイド](capture-group-conformance.ja.md)を参照する。
describeの成功だけで取得操作やデータ寿命の適合を証明しない。

- capture: features（0x05）はrevision 1でbitを定めないため、値0なら省略してよい。存在すればu32の形だけを確かめ、
  bitの値では失敗にしない（後から定義されうる、capture §3.5/§4.3）。
- track: planのrole_channels（0x01）かchannel_group（0x06）を1つ以上、modeの9 byte固定形とモードごとに1行、
  mode 1/3のmax_segmentsは0、rate_rangeの8 byteと1≤min_hz≤max_hz、channels u8、triggerの8 byteとtypesに即時（bit0）を含むこと。
  宣言するmodeは1つ以上で、1/2/3のどれを選ぶかは問わない。mode別に必須になるrelease/subscribeは[登録op検査](registered-ops-conformance.ja.md)が確かめる。
- analog: frontendの9 byte固定形（frontend、range_min_mv、range_max_mv）を1つ以上、番号ごとに1行。
- capture-group: tracksの要素数n≥1とu16列の形。
- config: items（0x41）の存在、1つ以上、重複なし、項目番号0を含まない（probe.config §4）。
  旧版のslots_max/wifi_maxは現仕様に無いため検査しない。

storage max_bytesとsave/erase提供時の存在は既存SelectedMetadataChecksで別に確認する。
itemsの未知tagを独自拡張でないと推測して禁止しない。

`CaptureMetadataModel`は17 interfaceの宣言だけを持つ独立fixture。
全fn共通宣言と登録opは別に確認する。
fixtureはfn 14にmode 1/3とsubscribe/unsubscribe、fn 15にmode 1/2とreleaseを宣言し、probeのchannelsを3にする。
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

one-shotの取得完了は`test_virtual_bench_capture_one_shot_time.py`の50件で別に確認する。
自然トリガー・force・即時取得、単独/group、logic/analogで、区画完成前のreadが空であること、
完成前にsegment/stopped完了通知を送らないこと、完成後の世代と通知の一回性を確認する。
途中stopでは経過したsampleだけを短い区画として残し、取得エラーでは未完成区画を公開しない。
実現rateが分数の場合も、最後のsampleとanalogの最後のchannelのskewが経過してから完了する。
1 sampleのlogic取得はstart時刻に完了でき、analogはstart offsetを待つ。
未完成区画のreadを空にするのはこのfixtureの保存方針であり、取得済みの一部を返す他のProbeを禁止する規則にはしない。
ナノ秒境界の12件もこのモデル時計の部品検査で、実機の時刻不確かさを0とする検査ではない。

仮想波形はsample indexから値を返す関数で、完成時・stop時に過去の値を生成する。
この検査はその関数の時刻付き入力を用い、実際の入力を継続観測して保存するbufferの証明ではない。
repeat/streamingのoffset/skewを含む完全な時間因果性、実機適合は別途確認する。


`test_virtual_bench_capture_channel_time.py`はchannel時刻とforce/groupを98件で確認する。
analogの条件判定はplan roleの数値ではなく、layout.orderでの位置から求めたskewまで待つ。
2 channelの疎なroleと4 channelの最後のroleで、base sample時刻とchannel時刻の間を照会する。
自然triggerの通知時刻はそのchannelで条件が成立した時刻。groupもこの時刻を保持し、
sourceを含む各trackのtrigger_indexを最寄りのbase sampleへ写す。同距離の選択方向は固定しない。
forceは実行時刻を通知し、offsetやsampleの丸めによってその時刻を動かさない。
最初のsampleより前ならindex 0、それ以外は最寄りsampleまでの距離を確認する。

repeat/streamingの24件はlogic/analog、1/2 channel、3 actual_rateで未来のchannel値を読まないことを確認する。
8件の不具合注入では取得用時計だけを24 sample分先へ進め、同じ時刻oracleが未来のdata生成を検出する。
Endpointを通した6件では整数msの時計とconfigureの広告skewを使い、source/group双方の通知時刻と世代を照合する。
残るpause/resume/stopの全境界、取得履歴buffer、実transport/物理時計は別途確認する。
