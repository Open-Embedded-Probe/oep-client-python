# 同一要求の再送と結果不明後の復帰

SPEC checkoutを明示し、`HostResendChecks(SampleHostResendAdapter(), spec=path).run()`を実行する。
core/transports本文（oep-spec a09fa45、待受け期限の検査と共有）と待ちの数値を照合する。private設備やOSの口を使わない。
既存SerialLink.send/exchange/resync/recover_transport、現在のvirtual Core、ResourceModelを
仮想時計と予定streamで接続する。事前に確定したmax_frame64/inflight2、所有セッション7/lease60000のfixture。
実時計で待たず、moduleのtime参照の一時差替えは必ず戻す。

fixtureのfn1/op16はresource(kind u8)を作り、成功payloadはid(u16)。op19は読むだけの状態。
標準OEP操作ではなく、coreの再送規則を試すサンプル拡張である。
8項目、7条件を検査する。

- LOST-COBS/LENGTH：Probeが実行した最初の応答を失わせる。Hostは待ちの下限後に同じ全byteを再送し、同じidを受ける。Probeは2回目の実行をせず、再送でleaseを更新しない。
- PIPELINE-COBS/LENGTH：2要求の2つ目の応答だけを失わせる。解決済みの1つ目を除き、未解決の2つ目だけを同じ全byteで再送する。resourceは2個のまま。
- BAD-CRC：所有中のCOBS口でCRCが壊れた応答を受け、同じ要求を送り直す。欠落時の待ち時間をこの破損応答へ要求しない。
- FAILURE-RECOVER：最初と再送の両応答を失わせ、TransportFailedを返す。次は復帰confirm後に読むだけの状態照会を行い、最初のresourceが既に存在することを確認する。元の状態変更を新しいcorrで繰り返さない。
- FAILURE-BLOCKED：復帰confirmも失わせる。経路はfailedのままで、状態照会も新しい状態変更もProbeへ送らない。

lengthは信頼できる非TCP経路のfixtureとして、実際のresyncも通す。
TCPの接続close/reopen、link自身のconfirm待ち時間、実clock/physical transportはこの結果に含めない。
破損/欠落時の再送回数はresend=Trueの現実装の採用設定。SPECがすべてのHostへ再送を必須にするという意味ではない。
待ちの下限はa09fa45のcore §4.4に従い、max_op_ms＋1秒＋転送時間（fixtureのop16は短い処理上限を定めない）。
fixtureのmax_op_msは2000 msで、reportの値から検査側が下限を独立に計算する（旧版はadapterの「引数0＋1秒」を信用していた）。
production Host.expect_msは引数時間だけを運ぶため、サンプルはSerialLink.expected_sへmax_op_msを明示的に与える。
転送時間は実writeしたframe長とmax_frame64から独立に求め、欠落時の再送開始が下限以上であることを確認する。
Probe側のreplay（再実行せず、leaseを更新しない）はcore §5.1/6.1（a09fa45）と一致する。byte記録は任意になったが、
このfixtureのvirtual Coreは記録を持つProbeとして振る舞う。
reply byteとresource next_id、live状態、lease deadlineをProbeイベントへ記録する。

単体27件：正常1、観測故障13、実link/Core故障6、旧下限（引数時間＋1秒）での早い再送4、fixtureのmax_op_ms欠落1、制御不足2。
corr/payloadの変更、結果変更、再実行/lease更新、早い再送、解決済み要求の再送、
failed flag/confirm省略、復帰前照会、復帰失敗後の継続、誤った結果を検出する。
fixtureが復帰後の新要求まで落としていた条件を修正し、productionや現在のCoreのコードは変更していない。
TCP close/reopen、実clock/transport、own-link confirm時間、破損時の再送上限、他pipeline/cache容量、
corrの循環と半周の制限（core §4.1/5.1、a09fa45）、production Hostによる処理上限の選択、
全production Host統合は残る。full_conformance=false。
