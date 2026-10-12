# 有限保持の通知出力：代表fixture

`tests/test_conformance_notification_output.py`は、循環corr作業版のcore §11.3/§11.4を確認する限定したraw fixtureである。
公開の汎用通知検査APIや、本番virtual profileを実装したものではない。
独自の`io.example.notice`にsubscribe/unsubscribeと固定58byte payloadのkind1イベントを定義し、通知の共通部分をOEP固有操作から分けて検査する。

```sh
uv run pytest tests/test_conformance_notification_output.py
```

fn7、SID1、event tokenはテスト用の選択である。実機のfn/pin/配線の仕様にはしない。
呼出し側が渡す刺激は`event(token)`、保持と出力は`BoundedOutput`、独立した復号は`decode(bytes)`で扱う。
実機へ移す場合は対象の操作・刺激・観測adapterを用意し、実driverの送信状況も記録する。

max_frame64、1inflight、cache0/1、通知用の1frame slot、1応答slotを使う。
通知を7byteだけ書き出してから読む要求を受け、残りを終えて応答を返す。部分frameの破棄や中への別frame挿入を許さない。
まだ始めていない通知と応答がある場合は応答を先にする。
slotが使用中に生まれたイベントを捨て、そのseqを消費し、次のイベントのseqに抜けを残す。
通知の1packetはlength込み66byteであり、max_frame×2=128byte以下。Python objectや全端点のRAM測定値ではない。

データ保持48byte/max_delay_ms0の代表条件で、min_bytes65535の購読をunavailable cause2で断り、既存条件/seqを維持する。
次の有効な購読はseq0から始め、unsubscribe後に新しいイベントを送らない。
senderの細分化、購読拒否、seqの抜け、応答優先に4つのfaultを注入して、それぞれのcaseで検出する。
reportには全raw入力、message交換、出力chunk、選択した観測と未終了SIDを含める。

1frame slotはこのfixtureの方針で、すべての実装に同じslot数を要求しない。
転送は論理的なlength streamで、OS/USB/UARTの実write、timed batching、データ通知、全購読寿命、長い取得中の状態、連続負荷を確認したものではない。
刺激と出力の順序を制御した代表caseであり、全適合を主張しない。reportはfull_conformance=false。
