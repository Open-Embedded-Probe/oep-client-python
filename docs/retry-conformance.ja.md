# 線の再試行の論理検査

OEP debug §2/4.1の検査を`RetryChecks`、明示的な論理fixtureを`RetryModel`と
`SampleRetryAdapter`で提供する。任意のOEP利用プロジェクトから公開Pythonライブラリだけで利用できる。
SPEC/registryは呼び出し元で明示的に選び、`Checks`へ渡す。私的な設備設定やベンチを探索しない。

三段階のうち、共通宣言は第二段階、今回の前提と動作は第三段階。
`full_conformance`はfalse。既存fixtureを変更せず、新fixtureを独立して実行する。

- hostのDMI手順は、届いた可能性があれば読み/書きとも繰り返さない。DATA書き込みと
  resumereqも対象。失敗した手順と後続をdoneに含めず、failed/partialで答える。
- busyは未受付として再試行できる。fixtureは最大2回。使い切ったらwaitで答える。
- probe自身のDMSTATUS読みはlineで再試行できる。fixtureは最大2回、2回目は一時的に
  半分の速さ。connectionのspeedとtarget状態は変えない。
- line失敗後のやり取りの間は駆動を止める。pull無しか正しいidle方向だけを認める。
  RVSWD idle_clock=1のclockにpull-upを認めない。成功後は正常idleへ戻す。
- このfixtureはlineだけではconnectionを閉じない。他connectionの成功で失敗側の
  ピンを戻さない。失敗応答の同一要求再送は保存結果を返し、線へ再送しない。
- 後ろの不正DMI手順も、線へ触れる前に拒否する。

`plan(parent,[0,1,2,3])`でok/wait/line/faultの結果列を渡す。0/2の書き込みは届いたものとして
モデル化し、再送の害を観測可能にする。faultでの配送判断はfixture条件であって規範ではない。
`probe_read(parent)`はprobe内部の読みを明示的に起こす刺激で、OEPの新しい操作ではない。
`idle(parent,0|1)`も論理fixture設定でありattach TLVの実装を意味しない。
state/traceにはpacketの速さ、配送判断、失敗後のmode/level/pullを保存する。

```sh
uv run pytest -q tests/test_conformance_retry.py
```

Arduino側の同じuv/pytest環境からは次を使う。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_retry.py
```

波形・電気特性・実時計は証明しない。線切れの判定と応答後の解放順、console link-lost、
resync/TO/framing、poll/wait内のpacket失敗、storeを起こす内部書き込み、他の再試行予算、
production Host統合は未検査。論理検査と実機適合を分けて記録する。
