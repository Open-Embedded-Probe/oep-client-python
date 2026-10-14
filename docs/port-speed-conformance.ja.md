# UART bridgeのport_speedを検査する

PortSpeedChecksはlink §3のProbe側の握手、phase表、復帰timerを、明示的な応答書き込み完了と論理時計で検査する。
PortSpeedModelは現在のCore・GPIO/plan・linkを組み合わせ、UART bridge0/2とTCP1を宣言する。
port_speed(0x12)とport_state(0x13)を宣言し、phaseを口ごとに持つ（0起動時、1試し、2決めた、3復帰保留）。
起動115200、fixtureが作れるrateは115200/230400/460800。
物理UARTは開かず、実際のHostがbaudを切り替えるプログラムの適合とは別に扱う。

共通InterfaceChecks、RegisteredOpsChecks、固有checkerの順で実行する。
固有reportはRegisteredOps2・context1・20 case、合計23項目。
registryのop18/19、両opのdescribe宣言、idle3000ms、許容差2%とUART bridge宣言を確認する。
RegisteredOpsChecksはport_speedを持つならport_stateも持つことを確認する。
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
adapterの`receive()`は、次のframeの受信が始まった時点を表す。続くbegin（またはraw）がそのframeを受け終える。
受信中または書き込み中に復帰条件が成り立つとphase3になり、そのframeの応答を旧baudで書き終えてから起動baudへ戻る。
port_stateは毎回新しいcorrで読み、過去のport_speed成功応答をphaseとして扱わない。
検査器はbufferのrole/corr/resolution/payloadをChecksで読むが、この検査操作をHostが応答を受け取った時刻としない。
prepared/sent/discardのeventと、その時のbaud・論理時刻を別に記録する。

| case | 確認 |
|---|---|
| TRY-BARRIER | 応答buffer準備・9ms待ちの間は115200。旧baudで応答を書き終えてから230400へ変更し、verifyの計時を始める。port_stateはphase1 |
| COMMIT | 試行後に確定し、応答の実baudとphase2。step1/2はverify_ms65535も0も受理 |
| REVERT-TRIAL / REVERT-COMMITTED | 応答のbaudは起動時の115200、書き終えるまでは元の230400、旧baudで応答送出後に復帰しphase0 |
| STATES | phase表の拒否：phase0のstep1/2、phase1のstep0と異baudのstep1、phase2のstep0/1はcause6で状態不変。各stepの後にport_stateでphaseを読む |
| VERIFY | 99/100ms境界でtrialを自動復帰し、port_stateはphase0 |
| TOLERANCE | 実230400に対する整数境界235102/225883を受理、隣235103/225882を拒否。要求232000でも応答の230400を使い、commitは試した232000を再び名指す |
| BOOT-RATE | 起動速度以下になる115200/113000/117000のstep0はunsupported（印0） |
| MALFORMED / TLV | 短い固定部分、step0のverify0、未定義step、未知criticalのecho、番号0、切れたTLV、未知optionalの無視。port_stateのcritical/optional TLVも確認 |
| REPLAY | 同じtry要求を20ms後に再送しても速度・verify deadline・保護状態を再変更しない |
| IDLE | 確定後2999/3000msで復帰し、port_stateはphase0 |
| IDLE-RENEW | 正常confirmとその応答でidle期限を更新し、更新後の2999/3000ms境界を確認 |
| IDLE-INVALID | 短い無効frameに応答せず、idle期限を更新せず、元の3000msで復帰 |
| END-BARRIER | end応答を書き終えるまではphase3の230400、応答は旧baudで送り、その後115200へ復帰 |
| NON-BRIDGE | sessionをTCPで開き、同じTCPからのport_speedとport_state（SID0/保持SID）をcause6で拒否 |
| ONE-PORT | port0がphase1/2/3の間、別bridgeのstep0/1/2はcause6でpeerも変えず、別bridgeのport_stateは115200/phase0 |
| PENDING-VERIFY | verify境界をまたぐframe：port_stateはphase3、所有sessionのstep0/1/2もcause6、無効frameも含め応答後に復帰 |
| PENDING-IDLE-OWNER | idle境界をまたぐ所有sessionのport_state/keepalive/stepは保留を取り消しphase2。step0/1はcause6、step2は115200で戻す。取消後は3000msを数え直す |
| PENDING-IDLE-OTHER | SID0のport_stateやsource、無効frameは取り消さず、phase3のまま旧baudで応答後に復帰 |

ONE-PORTは、同じowner IDの要求を別のbridgeへ入れる**Probeへの不正なHost刺激**である。
phase3の間は、別bridgeのframeを処理するあいだport0の受信を未完了のまま保ち、最後に無効frameで終える。
正常なHostはsessionの要求を1経路で送る。これをHostの経路切替のサンプルにしない。
正常caseのcommit/revertでは20ms待つ。state/refusal等のraw刺激も含むため、全Hostのwait/switch適合は主張しない。
step2は有効baudだけで試し、step2の無意味なbaudと許容差の優先関係を新しく定義しない。
phase表の拒否と許容差・起動速度以下のunsupportedが重なる要求の優先順位は規範に無く、検査しない（拒否の刺激は有効baudだけ）。
phase3を作る刺激は1つのframeの受信だけであり、送受信が同時に続く場合は扱わない。
begin中に別の要求を実行する並行性は、この順次fixtureの採用範囲に含めない。

単体は`uv run pytest -q tests/test_conformance_port_speed.py`。
81件は正常1、共通宣言1、wire故障20、意味的故障48、control不足10、port_state不在1。
書き込み前切替、実baud/状態/送出baud、timer早期/欠落、再送deadline更新、cause/tag、critical、
許容差と境界、pin変化、非bridge/第二port受理・拒否時のpeer変化、idle誤更新を注入して検出する。
port_stateの古いphase/baud・非bridge受理・critical無視、起動速度の受理、revertの誤baud、step1のverify0拒否、
試したbaudでのcommit拒否、phase2/3中の第二port受理、end中のphase3欠落、保留を作らない即時復帰、
保留中のstep受理、verify保留の取消、frame途中の切替、境界での復帰欠落、他sessionによる取消、
所有sessionで取り消さない、取消後のstep1受理・step2拒否も注入して検出する。
初期故障注入では、bufferを置換せず返り値だけ変えていたため通常送出へ効かなかった。実際の送信bufferも置換する。
検査の失敗時にも準備した書き込みを完了してからcleanupし、未完了writeのエラーで元の違反を隠さない。

無効frameでidleを更新する仮想モデルの違反を1FAILで再現し、応答を出さないdiscardでは更新しないよう修正した。
成功件数だけで完成としない（full_conformance=false）。
長いwrite待ち/同時要求/誤応答復旧、同時に続く送受信frame、他資源と経路条件、Hostのswitch/wait、物理UART/productionは残る。

## セッション解放と古いwrite完了

PortSpeedLifecycleChecksはcontext1と9ケースの部分検査を追加する。
共通宣言とPortSpeedChecksの後、独立なmodel/Checksを作り、同じ明示SPEC registryで実行する。
`PortSpeedLifecycleChecks(checks, SamplePortSpeedAdapter(model)).run()`を呼び、traceとreportを保存する。

- FORCE-TRIAL / FORCE-COMMITTED：forceで新しい保持者へ交代しplanを解放する。応答を書き終えるまではphase3の230400、応答は旧baudで送り終え、その後に起動baudへ戻る。
- EXPIRY-TRIAL / EXPIRY-COMMITTED：lease1000msの999/1000ms境界で保持者・plan・昇速を解放する。trial verify5000ms、committed idle3000msより先にleaseが切れる条件を使う。
- EXPIRED-TRY-WRITE / EXPIRED-COMMIT-WRITE：応答buffer準備後、write待ち中にleaseが切れる。古い応答の完了処理が速度・所有権・planを復活させない。
  commit応答の書き込み中ならphase3で230400を保ち、書き終えてから戻る。
- EXPIRY-PENDING / -OLD / -OPEN：次のframeの受信中にleaseが切れるとphase3。SID0のport_stateはphase3を返し、
  旧sessionのport_speedはcause6でなくno_session（coreの判定が先）、新sessionのopenも取り消さない。どれも応答後にphase0へ戻る。

各ケースの復帰後はSID0のport_stateで115200/phase0を読み返す。

このfixtureでは要求実行は瞬時、write待ちはidle時間として進む。forceの同時inflight、
実行中の時計停止、leaseが保留write中に切れた場合の物理送信成功は証明しない。
正常なforce例も新しいsessionを開く要求であり、保持者の既存sessionを他経路へ移動しない。
検査失敗時のcleanupは観測された保持者を使い、元の境界違反を別のno_session拒否で隠さない。

`uv run pytest -q tests/test_conformance_port_speed_lifecycle.py`：24件。
正常1、応答故障9、状態故障14。forceの早すぎる復帰/復帰欠落、lease早期・解放欠落、
古いwriteでtrial/commit/保持者を復活させる故障、commit応答の途中切替、
受信中の即時復帰、旧sessionへのcause6、session保留の取消・復帰欠落、forceのphase3欠落を検出する。

## 要求実行とwrite待ちの時計

PortSpeedExecutionChecksとSamplePortSpeedExecutionAdapterを、PortSpeedExecutionModelで実行する。
明示SPECから読み込んだregistryでChecksを作り、InterfaceChecksの後に実行する。
max_op_ms5000の宣言と明示execution controlが必要。部品はprivate設定を探索しない。

source/sink × 実行1500/3500ms × lease1000/60000msの8ケース、VERIFY、OTHER-ROUTEとcontext1を扱う。
400ms経過後に正常なframeを受け、実行中はcommitted idle期限とlease期限を進めない。
実行後のwrite待ちは、短leaseなら残り600msの599ms、長leaseならidle2999msまで進める。
期限が待ち中にずれないこと、応答完了からidle3000msを数え直し、leaseは応答生成時から数えて送信完了で変えないこと（core a09fa45 §6.1）、
その後の999/1000または2999/3000境界を確認する。
固定した既知sourceデータとsink応答、live GPIO/plan/所有権も照合する。

VERIFYは試し中にSID0のsource3500msを実行し、verify期限が実行時間だけ延び、応答では数え直さないことを確認する。
OTHER-ROUTEは確定後、別bridge2でSID0のsource3500msを実行し、port0のidle期限が延びて、その応答では更新されないことを確認する。
どちらも残り時間の1ms前/境界で復帰を確認する。

`uv run pytest -q tests/test_conformance_port_speed_execution.py`で40件。
正常全体1/各スケジュール10、応答故障10、時計・状態故障12、停止故障5、fixture guard2。
停止故障はverify消費、応答でのverify更新、他経路実行でのidle消費・更新、verifyの早期復帰。
旧モデルで全8ケースのidle消費違反を再現し、実行時間だけidle期限を延ばした。
3500msはidle3000msより長く、短lease1000msも越えるので、偶然期限内で通る刺激ではない。
複数経路の同時実行、source/sink以外の実行・拒否経路、物理時間・Host switch/waitは残る。
