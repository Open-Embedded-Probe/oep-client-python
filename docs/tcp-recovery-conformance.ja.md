# TCPフレームの途中休止と再接続

SPEC checkoutを明示し、`TcpRecoveryChecks(SampleTcpRecoveryAdapter(), spec=path).run()`を実行する。
core/transports本文SHAと待ち数値を照合する。private設備設定やOS socketを使わない。
実LengthFrames/Result decoderと現在のvirtual Core/ResourceModelを、仮想時計・TCP特性のstreamで接続する。
close/reopen/confirm/replayは明示的なサンプルHostの手順であり、production SerialLinkの再接続適合を示すものではない。

fn1/op16の資源作成をサンプル拡張として使う。max_frame64、boot17、session7/lease60000。
10項目、9条件を確認する。

- PAUSE-HEADER/BODY：keepaliveの後、長さ又は本文の途中で400ms休止する。100msのrecvでは未完了として返し、部分byteを保持する。次のrecvで元の結果を完成させ、接続を開き直さない。
- LENGTH：上限を超える長さを受ける。接続を閉じて新しいparserを作る。
- SHAPE：長さは収まるがresult headerに足りないフレームを受ける。同じ接続のquiet/confirmでは復帰させず、閉じて開き直す。
- EOF：streamが切断を通知する。開き直してから復帰する。
- BLOCKED：開き直した経路のconfirm応答も失う。旧要求を再送せず、接続を閉じる。
- EXPIRED：旧接続を閉じた後、lease60000msの期限で資源を解放する。再接続後の同じ要求には保存した成功応答を返すが、状態はロックなし・資源なし。新しい旧SID要求はno_sessionとなり、Hostは古い資源番号を有効とみなさない。
- REBOOT：旧接続を閉じた後、Endpoint.rebootとサンプル拡張の再初期化を通す。新boot18では旧要求を再送しない。fn0状態とno_sessionを確認し、listをページングしてfnを再発見してから拡張状態を読む。
- REBOOT-SAME：fixtureでboot17が繰り返される場合。boot_idだけでは再起動を検出できないが、ロックなし/no_sessionで旧セッション・資源を捨てる。サンプルはこの場合もlistをやり直す。

開き直した経路ではconfirmを最初に送り、そのcorr/header/magic/boot_idを確認してから元の要求全byteを再送する。
Probeのsession、資源、lease deadline、再送cacheを接続の寿命と分離し、再送の結果と非再実行を確認する。
保持cacheは内部状態のsnapshotで比較し、固定cache形式をwire仕様にはしない。
100/400msの一致は処理時間ゼロのfixtureの条件で、実CPUやネットワークへ正確な時刻を要求しない。

単体46件：正常1、観測故障34、実frames/Core/stream/Endpoint故障9、制御不足2。
最初のfixtureではconfirm要求を応答magicで作ってしまい、SIDを含む全headerの期待値とdecoder例外名にも誤りがあった。
現SPECのOEP?とmin/max revision、全header、実ShortPayloadへ修正した。production/Coreの変更はない。
listの実応答byteと状態照会・再発見の順序も照合する。期限切れ後の履歴の成功と、現在有効な資源を区別する。
任意のboot後カタログ/設定、leaseとの他の競合順、実OS TCP socket、production Hostの再接続手順、通知/同時接続は残る。
full_conformance=false。
