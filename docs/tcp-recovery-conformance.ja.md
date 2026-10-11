# TCPフレームの途中休止と再接続

SPEC checkoutを明示し、`TcpRecoveryChecks(SampleTcpRecoveryAdapter(), spec=path).run()`を実行する。
core/transports本文SHAと待ち数値を照合する。private設備設定やOS socketを使わない。
実LengthFrames/Result decoderと現在のvirtual Core/ResourceModelを、仮想時計・TCP特性のstreamで接続する。
close/reopen/confirm/replayは明示的なサンプルHostの手順であり、production SerialLinkの再接続適合を示すものではない。

fn1/op16の資源作成をサンプル拡張として使う。max_frame64、boot17、session7/lease60000。
7項目、6条件を確認する。

- PAUSE-HEADER/BODY：keepaliveの後、長さ又は本文の途中で400ms休止する。100msのrecvでは未完了として返し、部分byteを保持する。次のrecvで元の結果を完成させ、接続を開き直さない。
- LENGTH：上限を超える長さを受ける。接続を閉じて新しいparserを作る。
- SHAPE：長さは収まるがresult headerに足りないフレームを受ける。同じ接続のquiet/confirmでは復帰させず、閉じて開き直す。
- EOF：streamが切断を通知する。開き直してから復帰する。
- BLOCKED：開き直した経路のconfirm応答も失う。旧要求を再送せず、接続を閉じる。

開き直した経路ではconfirmを最初に送り、そのcorr/header/magic/boot_idを確認してから元の要求全byteを再送する。
Probeのsession、資源、lease deadline、再送cacheを接続の寿命と分離し、再送の結果と非再実行を確認する。
保持cacheは内部状態のsnapshotで比較し、固定cache形式をwire仕様にはしない。
100/400msの一致は処理時間ゼロのfixtureの条件で、実CPUやネットワークへ正確な時刻を要求しない。

単体27件：正常1、観測故障19、実frames/Core/stream故障5、制御不足2。
最初のfixtureではconfirm要求を応答magicで作ってしまい、SIDを含む全headerの期待値とdecoder例外名にも誤りがあった。
現SPECのOEP?とmin/max revision、全header、実ShortPayloadへ修正した。production/Coreの変更はない。
実OS TCP socket、production Hostの再接続手順、再接続中のreboot/lease期限切れ、通知/同時接続は残る。
full_conformance=false。
