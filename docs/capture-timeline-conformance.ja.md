# 異なるレートのgroup取得検査

`CaptureTimelineChecks`は、明示したSPECのcapture §2/3.3/4.1に基づく第三段階の検査。
`SampleCaptureTimelineAdapter`で独立`CaptureTimelineModel`の波形と時計を指定する。
私的ベンチ、兄弟checkout、既存Hostのcapture decoderは取り込まない。

このfixtureはplan=1、ADC=2、logic=3、group=4。
ADC role1/3とlogic role0/1を別channelへ割り当て、one-shotだけを宣言する。
同レートの[操作fixture](capture-group-conformance.ja.md)の設定・encoder・寿命処理を再利用し、
各trackのsample時刻を独立した有理数のgridで進める。
`program(fn, rise)`は立上がるsample index（Noneなら立上がらない）、
`advance_to(ns)`は起動からの論理時刻を指定する。取得中に波形を変更しない。
刺激用の制御であり、OEP wireへ新しいopを追加するものではない。

72構成で自然triggerとforceを検査する。

- sourceはADCまたはlogic。source/peerのrateは1000/333、333/1000、1000/500 Hz。
- sourceのpretriggerは0または2。自然triggerはsample index 1/3/7。
- forceは開始時、2.5ms、6.5ms。履歴不足や最寄りsampleが未到来の場合も含む。

自然trigger直前は待機を保ち、到来したsampleだけで条件を評価する。
各trackの到来済み履歴を保持し、forceが未来側の最寄りsampleへ対応する場合は、そのsampleの到来を待つ。
完了後にwireのconfigure/start/status/37 byte区画recordを取り出し、
[geometry checker](capture-group-geometry-conformance.ja.md)へ既知のgridとtrigger時刻を渡す。
P_kの切り上げ、最寄りindex、履歴不足によるshort、世代と開始時刻を照合する。
生のread結果も独立した波形計算と照合し、session end後のplan/pin/取得cache解放を確認する。

単体163件は正常72構成、wire応答故障72、意味的故障10、宣言2、不正刺激5、前提不足2。
先読み、履歴消去、レート変更、ADC/logicの値の誤り、最寄りindex、short、旧世代、
cache残留、sample欠落を注入して失敗を確認する。
全72構成をまとめたreportは294項目（登録op5・前提1・取得72・geometry216）。

`full_conformance`はfalse。既知の2 track/one-shotの論理fixtureを検査する。
repeat/streaming/通知、他のgroup構成・ADC layout/skew、最大pretrigger・serial保持境界、
非同期取得中のerror/再送/資源競合、物理clockの精度とproduction Hostは別途必要。
1nsの時刻照合幅はこのfixtureの整数ns表現に対するもの。
物理probeの精度保証や、報告uncertaintyに基づく許容幅ではない。

```sh
uv run pytest -q tests/test_conformance_capture_timeline.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_capture_timeline.py
```
