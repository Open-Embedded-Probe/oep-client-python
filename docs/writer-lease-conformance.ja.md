# 標準資源と応答writerのlease検査

`WriterLeaseChecks`はcore §5.2/6.1/9を第一段階で検査する。
`WriterLeaseModel`/`SampleWriterLeaseAdapter`はwire connectionとdmseq consoleを持つ明示logical fixture。
共通宣言41項目は別の第二段階、既存cutの9項目も別の第三段階として再実行する。
公開Pythonライブラリと明示したSPEC/registryだけで使え、私的ベンチや設備設定を探索しない。

前提1＋動作6の7項目。fixtureはlease 1000 ms、max_op_ms 5000 ms、単一writer。
`advance(ms)`は論理時計を進め、`execution(ms)`は次に実行する有効DMI要求の論理実行時間を指定する。
実時計sleepやUSB転送は使わない。時計はfixtureの継続カウンタで、bootのuptimeは端点側が分離する。

- 応答を生成した時点から1000 msで数え直し、writer待ち中と書き出しの完了では更新しない（core a09fa45 §6.1）。
  rejectedでもsession判定を通った応答は更新する。同一要求再送では更新しない。
- 実行時間1500 msはleaseより長いが、実行中は期限を数えない。応答の生成から数え、writer待ちでも数え続ける。
- writer停止中に期限へ達したら、ownerと子stream/protocol/mailbox、connection、pinを解放する。
  閉じる直前のtarget/halt状態を保つ。両connectionを解放する。streamはbufferごと解放し、markは残さない（console §2）。
- 線切れ判定済みのconnectionも、session期限切れの後始末として同じく閉じる。
- キューに残る応答は当初の結果のまま返す。古いwriter tokenで所有権や資源を復活させない。
  保存結果の再送は再実行しない。旧sessionの新要求はno_session、新sessionが所有中ならlocked。
  新資源は同bootの旧IDを使い回さない。

`closed_targets`は検査用の観測コピーで、live資源ではない。
lease経過は`tick()`で処理する。writerを停止したcaseはfinallyで必ず進めて後始末する。

```sh
uv run pytest -q tests/test_conformance_writer_lease.py
```

Arduinoのuv/pytest環境からは次を使う。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_writer_lease.py
```

実transportの送信完了、実時計誤差、writer並行性、forceとの競合、別予算/geometry、
production Host統合は未検査。`full_conformance`はfalse。
