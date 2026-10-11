# groupのpretriggerと時刻の検査

`CaptureGroupGeometryChecks(checks).run(...)`は、明示したSPECのcapture §2/3.3/4.1を第三段階で検査する。
configure/start/statusの成功payloadと各trackの最初の37 byte区画recordを、そのまま渡す。
probeのencoder、Host decoder、私的ベンチや兄弟checkoutは取り込まない。

呼出元はbind順のmembers、条件を持つsource_fn、そのpretrigger、各trackの最初のsampleの時刻基準、
独立に観測したtrigger時刻、forceの有無、時刻照合の許容幅を明示する。
履歴が途切れず取られたと独立に確認できるfixtureだけに使い、`continuous_history=True`を渡す。
その条件が不明なら、この検査の結果を履歴の正しさの証明に使わない。

3項目を返す。

- CONTEXT: actual_rateのnum/denとactual_samples、group startのbind順/世代/長さ/blockingを確認する。
- STATUS: one-shot完了、開始時刻/組の世代の一致、共通trigger時刻とsource（forceなら0）を確認する。
- SEGMENTS: sourceのPとactual_rateから、各trackのP_kを整数で切り上げる。
  生の区画の世代・初回serial/position・sample数/index/flagsと、既知のsample gridを照合する。

P_kの式は `ceil(P × num_k × den_t / (den_k × num_t))`。
中間積を64 bitで切り詰めず、実際のnum/denを使う。
triggerに最も近い非負のsample indexを選ぶ。半周期ちょうどでは前後のどちらも許す。
最初のsampleより前にforceした場合はindex 0へ写す。
残せる履歴は `min(P_k, closest_index)`。
最初の区画の総sample数、trigger_index、開始時刻とshortをその履歴量から確認する。

sample時刻の推定値は一般には実時間と完全には一致しない。
このcheckerの時刻照合幅はcallerが既知fixtureの分解能/条件から決める。
`start_uncertainty_ns`はSPEC上の保証ではないため、報告された値から許容幅を自動で広げない。
物理probeへこの論理fixtureの1ns許容幅をそのまま適用するものではない。

`virtual_capture_group_geometry_model.fixtures()`は41の生payload/record例を作る。
整数/分数rate、20MHz→48kHzの換算、P=0/2/7、履歴不足、半周期の両方向、最初のsample前のforce、
大きいu32 num/den/Pによる64 bit超過、3 trackと飛び番fnを含む。
これはmetadata例の生成器で、取得エンジンや大量のsample bufferを実装するmodelではない。
configure例はrate/capacity以外の通常の必須情報も持つが、全layout/scaleなどの検証は別checkerの担当。

単体107件は41例、30の不正観測/前提と算術の故障、既存仮想Endpointの36構成を含む。
Endpoint構成はlogic/analogのsource、3組の範囲内rate、P=0/2/7、自然/forceの組み合わせ。
実際の応答bodyをwireから取り、actual_rateとADC roleのskewから既知刺激の時刻を求めて照合する。
既存Endpoint/Hostのlegacy coreやop番号の適合を証明する検査ではない。
最初に使った333/833Hzなどはそのprofileの宣言範囲外だったため、1000/1500/1001/1333Hzへ直して確認する。

`full_conformance`はfalse。bind feasibility/max_pretriggerの宣言、全configure/data構造、
wire拒否/再送/全世代進行、repeat/streaming/通知、途切れた履歴、物理clock/skew/精度は未検査。
[独立group操作検査](capture-group-conformance.ja.md)と組み合わせるが、
その同レートfixtureを異なるrateの独立取得エンジンとして完成したとは扱わない。

```sh
uv run pytest -q tests/test_conformance_capture_group_geometry.py tests/test_conformance_capture_group.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_group_geometry.py
```
