# groupの即時開始と時刻の検査

`CaptureGroupGeometryChecks(checks).run(...)`は、明示したSPECのcapture §2/3.3/4.1を第三段階で検査する。
configure/start/statusの成功payloadと各trackの最初の37 byte区画recordを、そのまま渡す。
probeのencoder、Host decoder、私的ベンチや兄弟checkoutは取り込まない。

capture-group v1は即時の一括開始だけを定め、連動trigger/pretriggerと時刻からのtrigger_index写像を定義しない。
この検査もone-shotの即時開始だけを扱う。
呼出元はbind順のmembers、各trackの最初のsampleの時刻（独立に分かっている起点）、時刻照合の許容幅を明示する。

3項目を返す。

- CONTEXT: actual_rateのnum/denとactual_samples、group startの固定形（start_ns、組の世代、n、bind順のfnと世代）。
  start_nsは全bit1でなく、世代は0でない。
- STATUS: 13 byteの固定形、one-shot完了のstate 4、開始時刻/組の世代がstart応答と一致すること。
- SEGMENTS: 各trackの初回serial/position 0、track世代、sample数がactual_samplesと等しいこと、
  gap/short/予約flagsが無いこと（時間の基準が曲がったbit2は許す）、即時のtrigger_index。
  区画のstart_nsを既知の起点と許容幅で照合し、組のstart_nsより前に取っていないことを確かめる。

即時の区画のtrigger_indexは0xFFFFFFFF（トリガを含まない区画）か0を受け付け、観測値を記録する。
SPECは即時開始の区画がどちらになるかを明記していないため、片方へ絞らない。

sample時刻の推定値は一般には実時間と完全には一致しない。
このcheckerの時刻照合幅はcallerが既知fixtureの分解能/条件から決める。
`start_uncertainty_ns`はSPEC上の保証ではないため、報告された値から許容幅を自動で広げない。
物理probeへこの論理fixtureの1ns許容幅をそのまま適用するものではない。

`virtual_capture_group_geometry_model.fixtures()`は27の生payload/record例を作る。
整数/分数rate、20MHzと48kHzの組、trackごとの起点のずれ、trigger_index 0の例、
大きいu32 num/denとu64端に近いstart_ns、3 trackと飛び番fnを含む。
これはmetadata例の生成器で、取得エンジンや大量のsample bufferを実装するmodelではない。
configure例はrate/capacity以外の通常の必須情報も持つが、全layout/scaleなどの検証は別checkerの担当。

単体62件は27例、28の不正観測/前提（旧形式のstart/statusを含む）、報告不確かさの非利用、
既存仮想Endpointの6構成を含む。Endpoint構成はlogic/analogのbind順と3組の範囲内rate。
実際の応答bodyをwireから取り、組のstart_nsとADCの既知の変換遅れ（5000ns）から起点を求めて照合する。
既存Endpoint/Hostのlegacy coreやop番号の適合を証明する検査ではない。

`full_conformance`はfalse。wire拒否/再送/全世代進行、bindの実現可能性、全configure/data構造、
repeat/streaming/通知、stopの短い区画、物理clock/skew/精度は未検査。
[独立group操作検査](capture-group-conformance.ja.md)と組み合わせるが、
その同レートfixtureを異なるrateの独立取得エンジンとして完成したとは扱わない。

```sh
uv run pytest -q tests/test_conformance_capture_group_geometry.py tests/test_conformance_capture_group.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_group_geometry.py
```

[異レート取得timeline](capture-timeline-conformance.ja.md)は、このgeometry checkerを使わず、
自分で組の開始・各trackの区画と生データを確認する。
