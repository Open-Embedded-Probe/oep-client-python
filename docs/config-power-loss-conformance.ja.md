# 保存途中の電源断と丸ごとの復帰

SPEC registryを明示したChecksとConfigPowerLossModelを作り、InterfaceChecksの後に
`ConfigPowerLossChecks(checks, SampleConfigPowerLossAdapter(model)).run()`を実行する。
privateベンチや隣接SPECを自動探索しない。RegisteredOps2/context1/4ケース＝7項目、共通宣言33項目。
max_frame256、storage64、items plan/label/idle/disableの論理fixture。

設定の持ち方は2領域のbyte配列ジャーナルをこのfixtureで選んだ。
JSONの項目/識別子表、世代、CRC、末尾markerを持ち、現在の領域を残して他方へ1byteずつ書く。
どちらかの完全なrecordを読み、完成した新しい世代を復帰する。
これはfixture内部の持ち方で、OEPのwire形式や実firmwareの保存方式を定めない。
書込み先の初期化はこのモデルの一操作であり、実flashのerase途中やprogram中のbit tearを再現しない。
世代は内部counterであり、検査は1〜3の範囲。counter wrapや任意の破損復帰は未検査。

- FIRST：以前の保存なし。全byte境界で、保存なし又は新しい設定全体へ復帰する。
- REPLACE：以前の保存あり。書込み開始前、各byteを書いた後、fixtureのcommit marker完了後に電源断し、以前か新しい設定全体へ復帰する。
- REUSE：2回保存してから3回目を書く。両領域を再利用しても、以前の保存や新しい保存を選び、それより古い設定には戻さない。
- ACK：saveの成功応答を確認してから電源断する。次のbootでは成功した保存内容へ必ず復帰する。

old/newはplanのpreset、label、idle、disableをそれぞれ変える。
getの項目全体・state/hash・idleのpin・実行planを照合し、一部だけ混ざる設定を認めない。
以前のlive GPIO high/planは消し、保存presetを自動実行しない。
最初のwire要求より先に、仮想Coreの保持者/last session/cacheの観測を行う。
古いsave要求の再送はno_sessionで拒否し、保存を書き換えない。
次のbootでも同じ設定とdurable byte配列を保ち、途中の書込みを再開しない。

write_stepsはadapterが実際のrecord byte数を明示する検査制御で、規範の長さではない。
すべての0〜write_stepsを試し、途中のsaveには成功応答を返さない。
fixtureの最終markerを書き終えた境界ではnewを要求するが、一般の保存方式のcommit位置を定めない。
hashアルゴリズム/JSON/CRCをcheckerのoracleに使わず、wireで設定したold/newの全項目とget/stateの関係を比較する。
Core内部観測とbyte画像は仮想fixture用の補助制御であり、Probeへ新たな読取りAPIを要求しない。

単体30件：正常1、共通宣言1、wire故障4、状態/保存故障17、制御不足7。
active領域上書き/消去、誤った旧世代選択、commit/CRC破損後の成功、項目の混在、idle/preset、
同じbootの再使用、早い応答、復帰の書換え、旧保持者/履歴/cache、途中再開、volatileな未来設定への誤った復帰を検出する。
初期モデルがactive領域を上書きして以前の保存を失うことを再現し、他方の領域へ書く形へ修正した。

保存中の応答抑止/実行時間/lease、実flashのerase/program tear・摩耗・物理永続性、任意破損/内部counter wrap、
全項目とproduction実装追従は残る。full_conformance=false。
