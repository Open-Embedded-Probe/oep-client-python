# 線切れの応答と解放の論理検査

`CutChecks`と独立した`CutModel`/`SampleCutAdapter`はOEP debug §2、common §1/2、
core §5.2/9を検査する。共通宣言41項目は第二段階、前提1＋動作8項目は第三段階。
今回の動作caseはRVSWD接続のRISC-V DMIとdmseq consoleを使う。
公開Pythonライブラリだけで利用でき、SPEC/registryは呼び出し側で選ぶ。
`full_conformance`はfalse。設備設定や私的ベンチを探索しない。

fixtureの線切れ方針は「成功した操作を挟まずlineの操作が2回続いたとき」。要求内の
個々のpacketやconsole内部の再試行は別々の操作に数えない。この閾値は規範ではない。
一度のlineだけではconnectionを閉じず、成功した操作で履歴を消す。拒否やtimerだけで
線切れを決めない。内部packetの曖昧配送とピン休止は[retry fixture](retry-conformance.ja.md)を使う。

- 要求中に判定したら、status lineのfailed/partial応答を書き出すまでconnectionと子streamを保持する。
  その後、子stream/protocol/mailbox、connection、ピンの順に解放する。
- `accept(request)`は要求を処理して応答writerを止める刺激。応答本文をhostへ返さず、ready/corrだけを返す。
  同じ要求を`Checks.exchange`へ渡すとwriterを進める。raw要求/応答はこの交換で記録する。
- 閉じた後の同一要求再送は保存結果を返す。線へ再送せず、資源を復活させない。新しい要求で旧IDを使うとno_resource。
- console読み中に判定したら、現在positionにlink-lost（8）、closed/connection_closed（9/4）を付けて閉じる。
  要求中の判定にはconsole専用のlink-lostを付けない。
- targetは閉じる直前のhost操作の状態に保つ。止まったhartも勝手に走らせない。
  別connectionとそのstream、target状態へ影響を与えない。閉じたpinにはidle駆動を残さない。

`poll_cut(stream)`はDMSTATUS読みと線切れ判定を起こす明示的console刺激。
`tick()`はtimer刺激。writer待ちの間は別要求とconsole刺激を拒否する単一writer fixtureであり、
並行実行モデルではない。状態とadapter traceへ判定/response-write/mark/解放の順序を記録する。
`archives`と`closed_targets`は検査用にコピーした観測記録で、閉じた資源をhostへ提供する仕組みではない。
live stream、受信/送信バッファとprotocol/mailboxは解放し、旧streamへのreadは拒否する。

```sh
uv run pytest -q tests/test_conformance_cut.py
```

Arduinoのuv/pytest環境からは次を使う。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_cut.py
```

ここでのresponse-writeは論理writerへの引き渡しであり、USB/TCPの実配送や物理pinを証明しない。
SWIO/SWD固有の切断経路、writer並行性、writer待ち中のlease期限、他の閾値/予算、resync/TO/framing、poll/wait内packet失敗、
storeを起こす内部書き込みとproduction Host統合は未検査。

writer待ち中のlease境界は、追加の[明示logical clock検査](writer-lease-conformance.ja.md)で扱う。
session終了によるcloseはsession-ended 2、通常のconnection切断はconnection-closed 4として区別する。
