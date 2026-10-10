# plan所有UARTへ適用する共通stream検査

`UartStreamChecks`は第二段階15項目。`SampleUartStreamAdapter.adoption`でfn 2とread/marks/clear/mark/write、
byte ring 64 / mark ring 5 / TX queue 4という採用条件を明示する。
既存StreamChecksの位置・ページング・あふれ・mark cursor/一周/eviction・u64境界のassertを再利用する。
clearの再送とwrite受付/結果/prefix/再送も確認する。

元の独立sampleはsession endでも受信bufferを保持するが、UARTはplanの資源としてendで消える。
そのsample固有の持続性をUARTへ誤適用しない。各条件ではwire open/plan_applyを記録してから検査し、
wire endで後始末する。UARTのconfigure/plan寿命は第三段階の検査へ分ける。

logical restart後、未所有のfixtureへu64位置/u32通番のseedを設定する。
外部markの注入は共通部品のcursor/selector用の刺激であり、UARTがtarget resetを実際に検出した証拠ではない。
位置/mark刺激とwire交換、planの作成/後始末はすべてtraceへ記録する。
物理配送、同時producer、通知data、別ring/frame geometryは未検査。`full_conformance`はfalse。
公開側は私的ベンチ・設備設定に依存しない。

```sh
uv run pytest -q tests/test_conformance_uart_stream.py
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_uart_stream.py
```
