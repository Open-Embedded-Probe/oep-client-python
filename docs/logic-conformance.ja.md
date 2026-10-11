# logic captureの論理検査

`LogicChecks(checks, adapter).run()` は選択SPECのcapture §1〜3、plan §2、core §6/8/9を第三段階で検査する。
`Checks(send, registry, unit)` と `SampleLogicAdapter(source)` を明示する。
公開側は私的ベンチ・隣接リポジトリ・設備設定を探索しない。
前提と標準op宣言を含む33項目。共通宣言17項目とlogic metadata4項目は別に適用する。

- state 0、plan無しconfigure/query/startの拒否、空readとstop、logicがpinの電気状態を変えないことを確認する。
- queryは設定を適用せず、configureと同じ実際の値を返す。必須TLV/幅/省略契約/0値/raw tag/first-winsを確認する。
- exact rate範囲、samplesの容量への切り下げ、repeatの区画数、trigger type/role/value、pretriggerの境界を確認する。
- edge triggerの待ちと履歴、取得した2チャネルの各値をwidth 3のbit格納で読み返す。
  未割当bitは意図的に1にし、checkerはposに当たるbitだけを見る。末尾の未使用bitも0を要求しない。
- startの世代、初回1・FFFFFFFFから1、旧世代read/releaseのcause 6、次startでのデータ消去を確認する。
  configureやplan解放では世代を戻さない。future readは空を要求し、応答positionを一つの値に固定しない。
- readのmax/more/continuation/max0、SID 0読出しと副作用なし、37 byte区画のページングとinclusive/next cursorを確認する。
- repeat満杯ではstate 5。releaseはinclusiveで完了済み区画を解放し、state 3へ自動で戻り、再開区画へgapを付ける。
  連続区画のpositionとstart_ns、release後のread gapを確認する。
- 途中stopはstate 1とshort区画。取得エラーはstate 6/error/flagsを示し、不完全な区画を捨て、完了済みdataを取り消さない。
  エラー後startは世代を進め、flagsをクリアする。
- start/releaseの同一要求再送で世代・data・解放を繰り返さない。
  plan置換/release/end/expiry/forceは設定・data・区画・取得途中を捨て、state 0へ戻す。

独立`LogicModel`はone-shot/repeat、queryあり、force/subscribeなしの論理fixture。
width 3、2チャネル（role 0/1、channel 0/1）、samples上限16、repeat区画上限2、
rate 100〜1000 Hzのexact値、pretrigger上限8。pinのidle出力もそのまま聞く。
`feed`はplan role順の明示0/1サンプルで、取得した数に応じてlogical clockを進める。
Fractionでsample周期を累積し、clockをlogical msへ落とす。区画の時刻不確かさは1 ms。
`advance`/`fail`/`seed`は明示刺激であり、物理sample sourceや実時間ではない。
wire交換・sample・state・generation・data・区画・pinをtraceに保存する。

単体54件でwire/packing、query適用、世代/区画/再開/gap/short/error/pinの誤りと再送の二重副作用を検出する。
不正刺激、前提不足、未割当bitとfuture空応答の未指定positionも確認する。
`full_conformance`はfalse。analog/calibration、group、streaming/通知、multirate、optional force/subscribe、
取得中query、早いtriggerで履歴不足、他packing/rate/trigger、serial wrap/metadata独立寿命、
永続config、並行性、物理sample/timing/不確かさ保証、production Hostは未検査。

```sh
uv run pytest -q tests/test_conformance_logic.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_logic.py
```

## 既存仮想ベンチのrate契約

capture §3.3のrateは宣言範囲内で実現可能な最も近い値を使う。要求以下へ丸めるとは限らない。
`tests/test_virtual_bench_capture_rates.py`は既存VirtualCaptureのsource/divisorという内部選択を明示し、
小さい範囲では実現可能値を全列挙する独立oracleで全要求を検査する。
logicの上/下丸め・同距離・min境界、analogの共有ADC予算と分数の上限、u32 source境界、
exact宣言、queryの非適用、wire actual_rateとanalog skewを確認する。
同距離の二候補は両方を許容する。このsource固有のdivisorを他のOEP probeへ要求しない。

これはproduction用仮想ベンチの追加回帰であり、上記独立LogicModelのexact rate検査とは別の証拠。
全analog取得/校正・capture-group・streamingの第三段階検査や物理clock保証を代替しない。

```sh
uv run pytest -q tests/test_virtual_bench_capture_rates.py tests/test_virtual_bench_capture.py
```
