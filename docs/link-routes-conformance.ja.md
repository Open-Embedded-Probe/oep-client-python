# 同じProbeの複数経路でlinkを検査する

LinkRoutesChecksは、core §4.4/5/6、transports §3、link §2を、1つのCoreを持つ明示仮想モデルで検査する。
経路0はUART bridge・max_frame64、1はvendor bulk・256、2はTCP・1024。
これらは論理経路であり、実際のシリアル・USB・socketを開かない。
LinkTransferModelのGPIO/planを保護状態として共有し、転送が既存のplan・高出力pin・所有権を変えないことを確認する。
単一経路の3個体で代用せず、同じboot_id・fn一覧・Core・再送表を使う。

採用adapterは経路選択、論理close/reopen、snapshot、経路付きwire traceを明示する。
TCPの接続世代はepochで保存する。closeは配送経路を閉じるだけで、session・plan・再送履歴を解放しない。
reopen後にconfirm・unit_idを確認し、同じboot_idと保護状態を要求する。
実際のsocket/USB閉鎖・古い受信byteの破棄・transport framingの証明とは区別する。

```python
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_link_routes import LinkRoutesChecks
from oep_client.conformance_link_routes_sample import SampleLinkRoutesAdapter
from oep_client.virtual_link_routes_model import LinkRoutesModel

registry, identity = spec_identity("/explicit/path/to/oep-spec")
model = LinkRoutesModel(lambda: 0, boot_id=17)
checks = Checks(lambda raw: model.handle_route(raw, 2), registry, "virtual-resource-1")
report = LinkRoutesChecks(checks, SampleLinkRoutesAdapter(model)).run()
assert report["status"] == "passed"
# identity、report、source/adapterの版を明示出力先へ保存する。
```

第二段階のInterfaceChecksは各経路に適用する。固有checkerにもRegisteredOpsChecksを含める。
contextは全経路のconfirm transport/index/limit、同じboot_id・unit・fn/instance/revision、
fn0の全経路宣言、任意port_speed不在と明示保存前提を検査する。
本文の仕様をもとにsourceの長さ・patternとsinkをraw交換で比較し、snapshotも保存する。

| case | 検査 |
|---|---|
| ROUTE-LIMITS | 小→大→中など経路を交互に選び、各経路の0/1/上限/u32最大sourceと最大sinkを検査 |
| FREE-CACHE | SID0の同一raw要求・同じcorrを別経路へ順次送り、その経路の上限で異なる応答を得る。SID0を共通表へ誤登録しない |
| SHARED-LOCK | 全経路のlock_stateが保持中。別IDのopenはlocked、SID0転送は成功し、live plan/pinを変えない |
| RECONNECT | TCPを論理close/reopenしてもsession/plan/pinは保持され、保持者の新規転送が使える |
| REPLAY-RECONNECT | 保存範囲内の16 byte sourceをTCP再接続後に同じbyteで再送し、応答を保持・非再実行。同corrの変更はmalformed |
| END-SHARED | sessionのendで全経路のlock_stateが空き、plan/出力がidleへ戻り、SID0転送が使える |

**Hostの経路規則**：1つのsession IDの要求は1つの経路で送る。
このfixtureも、保持者のopen・plan・GPIO・link・endをすべてTCP経路2に揃える。
他経路への転送はSID0、他経路のopen拒否は別のsession IDを使う。
再接続も同じTCPの論理経路で行い、保持者の要求を別の生きた経路へ移す例にしない。
FREE-CACHEの固定corrはProbeのSID0非重複排除を検査する順次刺激で、Hostのcorr allocatorの適合は別の責任。

単体は`uv run pytest -q tests/test_conformance_link_routes.py`。
30件は正常1、各経路の共通宣言3、wire故障6、意味的故障15、control不足4、
保持者が1経路を使うこととSID0の別経路応答を調べる独立監査1。
意味的故障は上限/transport/boot/fn一覧、global source長、pin変化、SID0誤cache、
経路別の独立ロック、closeでsession/cache消去・boot変更、reopenでplan消去、
再送時pin変化・変更要求受理、peer経路のend後ロック残留を含む。
所有権をcloseで失う故障は、checkerのcleanupが元の違反を隠さないこともassertする。

部分report（full_conformance=false）。true concurrentな複数経路のwindow/inflight、
通知先・revision選択、他資源、port_speedの握手/期限、productionと実機は未証明。
公開checkerはprivate台帳・隣接SPECを自動探索しない。外部adapterの観測sourceと版も別途保存する。
[単一経路の転送検査](link-transfer-conformance.ja.md)の全ケースを多経路へ適用した証明ではない。
