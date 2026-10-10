# GPIOとplanの論理動作検査

`GpioChecks(checks, adapter).run()`は、明示した論理fixtureへ第三段階の検査を行う。
fn 1がplan、fn 2/3がGPIO（instance 0/1）。操作code/tagは選択registryから読む。
外部プロジェクトは独自adapterで観測を供給できるが、この検査の刺激は下記fixture条件に限定する。
公開コードは私的ベンチや設備の暗黙探索へ依存しない。

独立GpioModelは8 channel、GPIO候補0〜7、全7 mode、同時plan 4割当を持つ。
channel 0はidle出力high、その他は入力。外部入力値はchannel番号の偶奇。
標準fixtureはdrive 2/8 mAの2段、default 0、channel 0のidle drive 1。他channelは0。
強さ切替なしの別条件も単体で確認する。数値mAと電気的特性は論理モデルの証明対象ではない。
明示logical restartで新bootに戻し、Coreのsession/replay機構を使用する。

23動作条件と登録op前提を含む28項目を検査する。

- plan取得では最初のsetまでidleを保つ。fn別置換は他fnを保ち、置換するfnの旧割当を除いて競合確認する。
- planの重複/無効fn/role/channel/容量/取り合いを、副作用前に拒否する。
- GPIO setは全要素を確かめてから要求順に駆動する。同じchannelをlow→highにする順序もjournalで観測する。
- set/readの未割当channel、未宣言mode、driveのindex/重複/mode/値/幅、未知critical TLVを確認する。
  channel/index/raw tagを応答で照合し、拒否前後のplan/pin/journalが完全に同じことを確認する。
- per-element drive、default sentinel、idle driveへのfallback、宣言なしでのdrive拒否を検査する。
- readはSID 0で要求順と重複を保ち、資源状態を変えない。成功応答の追加TLVを許す。
- 同一要求の再送でplan適用や駆動を繰り返さない。
- 個別/all release、plan置換、session endで旧ピンをidleへ戻す。

新fixtureの共通宣言25項目、2 GPIOのmetadata 7項目も別に実行する。
38単体テストは、誤ったwire outcome/reason、順序逆転、拒否前副作用、再送時二重駆動、idle復元漏れの故障を検出する。
物理level/pull/電流/波形、他fixtureのrole/channel_group、永続config、GPIO planでのforce/expiryは未検査。
この結果で全標準opや全plan条件の適合とはしない。`full_conformance`はfalse。

```sh
uv run pytest -q tests/test_conformance_gpio.py
```

Arduinoのuv/pytest環境からは公開clientのpathを明示する。

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_gpio.py
```
