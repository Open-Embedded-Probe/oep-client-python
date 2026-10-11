# UART bridgeのport_speedを検査する

PortSpeedChecksはlink §3のProbe側の握手と初期timerを、明示的な応答書き込み完了と論理時計で検査する。
PortSpeedModelは現在のCore・GPIO/plan・linkを組み合わせ、UART bridge0/2とTCP1を宣言する。
起動115200、fixtureが作れるrateは115200/230400/460800。
物理UARTは開かず、実際のHostがbaudを切り替えるプログラムの適合とは別に扱う。

共通InterfaceChecks、RegisteredOpsChecks、固有checkerの順で実行する。
固有reportはRegisteredOps2・context1・16 case、合計19項目。
registryのop18、idle3000ms、許容差2%とUART bridge宣言を確認する。
public checkerはSPECを明示選択し、privateベンチや隣接checkoutを自動探索しない。

```python
from oep_client.conformance import Checks, spec_identity
from oep_client.conformance_interfaces import InterfaceChecks
from oep_client.conformance_port_speed import PortSpeedChecks
from oep_client.conformance_port_speed_sample import SamplePortSpeedAdapter
from oep_client.virtual_port_speed_model import PortSpeedModel

registry, identity = spec_identity("/explicit/path/to/oep-spec")
model = PortSpeedModel(boot_id=17)
checks = Checks(model.handle, registry, "virtual-resource-1")
common = InterfaceChecks(checks).run()
report = PortSpeedChecks(checks, SamplePortSpeedAdapter(model)).run()
assert common["status"] == report["status"] == "passed"
# identity、report、checks.trace、source/adapterの版を明示した出力先へ保存する。
```

各ケースは論理再起動して新しいboot_idを読み返し、長いleaseでsession・GPIO高出力planを作る。
step要求の前後でplan/pin/idle/journal/所有権の不変を照合し、要求・応答とsnapshotを保存する。
PortSpeedModel.beginは応答bufferを用意するだけ。finishがその応答を書き終えた時点を表す。
検査器はbufferのrole/corr/resolution/payloadをChecksで読むが、この検査操作をHostが応答を受け取った時刻としない。
prepared/sent/discardのeventと、その時のbaud・論理時刻を別に記録する。

| case | 確認 |
|---|---|
| TRY-BARRIER | 応答buffer準備・9ms待ちの間は115200。旧baudで応答を書き終えてから230400へ変更し、verifyの計時を始める |
| COMMIT | 試行後に確定し、応答の実baudとstate2。verify_ms65535も受理 |
| REVERT-TRIAL / REVERT-COMMITTED | 応答のbaudは115200、書き終えるまでは元の230400、旧baudで応答送出後に復帰 |
| STATES | 起動中のcommit/revert、trial/committed中のtry、二重commit、異baudのcommitはcause6 |
| VERIFY | 99/100ms境界でtrialを自動復帰 |
| TOLERANCE | 実230400に対する整数境界235102/225883を受理、隣235103/225882を拒否。要求232000でも応答の230400を使う |
| MALFORMED / TLV | 短い固定部分、verify0、未定義step、未知criticalのecho、番号0、切れたTLV、未知optionalの無視 |
| REPLAY | 同じtry要求を20ms後に再送しても速度・verify deadline・保護状態を再変更しない |
| IDLE | 確定後2999/3000msで復帰 |
| IDLE-RENEW | 正常confirmとその応答でidle期限を更新し、更新後の2999/3000ms境界を確認 |
| IDLE-INVALID | 短い無効frameに応答せず、idle期限を更新せず、元の3000msで復帰 |
| END-BARRIER | end応答を書き終えるまでは230400、応答は旧baudで送り、その後115200へ復帰 |
| NON-BRIDGE | sessionをTCPで開き、同じTCPからのport_speedをcause6で拒否 |
| ONE-PORT | 他のbridgeでtryしても2つ目の口を上げずcause6。拒否時にpeerの速度も変えない |

ONE-PORTは、同じowner IDの要求を別のbridgeへ入れる**Probeへの不正なHost刺激**である。
正常なHostはsessionの要求を1経路で送る。これをHostの経路切替のサンプルにしない。
正常caseのcommit/revertでは20ms待つ。state/refusal等のraw刺激も含むため、全Hostのwait/switch適合は主張しない。
step2は有効baudだけで試し、step2の無意味なbaudと許容差の優先関係を新しく定義しない。
begin中に別の要求を実行する並行性は、この順次fixtureの採用範囲に含めない。

単体は`uv run pytest -q tests/test_conformance_port_speed.py`。
52件は正常1、共通宣言1、wire故障16、意味的故障25、control不足9。
書き込み前切替、実baud/状態/送出baud、timer早期/欠落、再送deadline更新、cause/tag、critical、
許容差と境界、pin変化、非bridge/第二port受理・拒否時のpeer変化、idle誤更新を注入して検出する。
初期故障注入では、bufferを置換せず返り値だけ変えていたため通常送出へ効かなかった。実際の送信bufferも置換する。
検査の失敗時にも準備した書き込みを完了してからcleanupし、未完了writeのエラーで元の違反を隠さない。

無効frameでidleを更新する仮想モデルの違反を1FAILで再現し、応答を出さないdiscardでは更新しないよう修正した。
成功件数だけで完成としない（full_conformance=false）。要求の実行中のidle停止、
長いwrite待ち/同時要求/誤応答復旧、他資源と経路条件、Hostのswitch/wait、物理UART/productionは残る。

## セッション解放と古いwrite完了

PortSpeedLifecycleChecksはcontext1と6ケースの部分検査を追加する。
共通宣言とPortSpeedChecksの後、独立なmodel/Checksを作り、同じ明示SPEC registryで実行する。
`PortSpeedLifecycleChecks(checks, SamplePortSpeedAdapter(model)).run()`を呼び、traceとreportを保存する。

- FORCE-TRIAL / FORCE-COMMITTED：forceで新しい保持者へ交代しplanを解放する。応答は旧baudで送り終え、その後に起動baudへ戻る。
- EXPIRY-TRIAL / EXPIRY-COMMITTED：lease1000msの999/1000ms境界で保持者・plan・昇速を解放する。trial verify5000ms、committed idle3000msより先にleaseが切れる条件を使う。
- EXPIRED-TRY-WRITE / EXPIRED-COMMIT-WRITE：応答buffer準備後、write待ち中にleaseが切れる。古い応答の完了処理が速度・所有権・planを復活させない。

このfixtureでは要求実行は瞬時、write待ちはidle時間として進む。forceの同時inflight、
実行中の時計停止、leaseが保留write中に切れた場合の物理送信成功は証明しない。
正常なforce例も新しいsessionを開く要求であり、保持者の既存sessionを他経路へ移動しない。
検査失敗時のcleanupは観測された保持者を使い、元の境界違反を別のno_session拒否で隠さない。

`uv run pytest -q tests/test_conformance_port_speed_lifecycle.py`：15件。
正常1、応答故障6、状態故障8。forceの早すぎる復帰/復帰欠落、lease早期・解放欠落、
古いwriteでtrial/commit/保持者を復活させる故障を検出する。
