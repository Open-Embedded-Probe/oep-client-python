# GPIO planを使ったcore寿命検査

`GpioLifecycleChecks(checks, adapter).run()`は第一段階のcore規則を、明示したGPIO plan資源で検査する。
fn 1 plan、fn 2/3 GPIO、8 channel、4割当とidle/driveはGPIO fixtureの契約に従う。
操作code/tagは選択registryから読む。公開側は私的ベンチへ依存しない。

`GpioLifecycleModel`はGpioModelに明示logical ms時計を加えた独立fixture。
各条件で明示logical restartにより新bootへ戻す。待ち時間をsleepで近似しない。
前提と条件をcoreとして報告し、GPIOの固有動作と共通宣言は別に検査する。

- lease 1000 msの999 msまではpin/planを保持し、1000 msで両GPIOのplanを解き全pinをidleへ戻す。
- 期限切れ後は同一set/plan要求に保存した成功を返せるが、資源を再確保・駆動・lease更新してはならない。
  新しい旧IDのsetはno_session。
- end後の同じIDはforce有無によらず新規openできない。
- 同じIDの新しいopenはleaseを更新し、GPIO plan/driveと再送履歴を保つ。
- forceなしの別IDはlocked。forceでは旧planを解放して新しい所有者へ渡す。
  旧要求は新所有者へ触れずlockedとなり、新所有者がendした後も旧成功履歴を復元しない。
- keepaliveとsession内の拒否応答はGPIO状態を保ってleaseを更新する。
- SID 0 readと保存応答の再送はleaseを延ばさず、setを再実行しない。

24単体テストは早過ぎる期限切れ、idle復元漏れ、lease非更新、履歴消失、期限切れ後の再実行、
同じIDでの資源解放、SID 0による更新などの故障を検出する。
実pinの時刻・永続config・deferred writerとforceの並行性・他fixtureのrole条件は未検査。
`full_conformance`はfalseで、完全なcore/全標準op適合とは扱わない。

```sh
uv run pytest -q tests/test_conformance_gpio_lifecycle.py
```

Arduinoのuv/pytest環境からも公開clientのpathを明示する。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_gpio_lifecycle.py
```
