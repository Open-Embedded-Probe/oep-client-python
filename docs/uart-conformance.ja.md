# UARTの論理動作とplan寿命

`UartChecks(checks, adapter).run()`は明示したplan fn 1 / UART fn 2を第三段階で検査する。
登録opの前提を含む29項目。code/tagは選択registryから読む。公開コードは私的ベンチに依存しない。

独立UartModelは8 channel中RX 0/TX 1を候補とし、両方/片方のplanを扱う。
空きはHi-Z。plan取得時にストリームを作りTXをhighへ保つ。
受信ring 64 byte、mark ring 5件、送信queue 4 byte、format 0/4/17。
論理divider clockは1843200 Hz（整数分周1〜65535）。configureは±5%以内の実際値を返す。未configureのstatusはbaud 0、format 0。
1755429/1940210 baudの要求は1843200で許容範囲、隣の1755428/1940211は範囲外として拒否する。
時計は明示logical ms。物理UARTの実現性や電流をこのfixtureで証明しない。

- configure前はTXをidleに保ち、受信しない（status 0/0、write拒否）。configure後に受信を始める。RX-onlyのwrite拒否、TX-onlyのconfigure/write。
- configureの初期化失敗（論理fixtureの制御で起こす）はbaud 0のcompleted failed。未configureへ戻り、未送信TXを捨て、受信済みbyteは読める。新しいconfigureで回復する。
- configure/status、format省略で8N1、宣言しないformat/予約値、raw critical tag、固定幅とfirst-wins。
- 拒否前後のstream/config/pin状態不変、未知TLVとmalformed/count、ロックの要否。
- 再configureは貯めた受信byte/位置/markを保つ。
- readの順序・非消費性・more/gap、あふれ、framing/parity lost、clear/host mark。
- writeのprefix受付とsuccess/partial/failed、ゼロ長、再送によるqueue非重複。logical drainは物理配送を意味しない。
- configure済みの割り当て変更はunavailable cause 6で、何も変えない。plan解放/再作成でデータとsession configureを捨てるが、同じboot内のbyte位置とmark通番を戻さない。
- end/expiry/forceでstream/queue/pinを解放し、位置を保つ。force後の旧要求は新所有者へ触れない。

共通streamの細かい境界は[採用部品検査](uart-stream-conformance.ja.md)で第二段階として別に確認する。
他のpin候補/channel_group、writer並行性、
物理baud/波形/配送とproduction Hostは未検査。`full_conformance`はfalse。

```sh
uv run pytest -q tests/test_conformance_uart.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_uart.py
```
