# Probe restartの部分適合検査

RestartChecksはrestart §1/2とcore §9に基づき、現在のCoreとGPIO/planを使うRestartModelへ操作を送る。
モデルの再起動は既存Endpoint.rebootを使い、boot/時計/Core/拡張資源を新しい起動へ切り替える。
明示SPEC registryでChecksを作り、共通InterfaceChecks、RegisteredOpsChecks、固有検査の順で実行する。
公開部品はprivateベンチや設備を自動探索しない。

SampleRestartAdapterは明示的なwrite完了と論理時計、3経路へのraw配送を提供する。
contextでrestart op16とrestart_max_ms50を確認する。
restart_delay_ms50はこの仮想fixtureが遅らせる実際の再起動時間であり、制御条件として明示する。
SPECのrestart_max_msは上限であり、一般のProbeが49ms応答を止め続ける義務を意味しない。
すぐ再起動するProbeへそのまま使う実機検査器ではない。

- BARRIER：GPIO高出力planをseedし、restartの応答buffer準備と9msのwrite待ちでboot/所有権/pin/planが変わらないことを確認する。旧bootで成功応答を書き終えた後、再起動区間へ入る。
- LOCK：SID0はsession_required、別SIDはlocked。拒否でboot/再起動/資源/保持者を変えない。
- TLV：番号0・切れたTLVはmalformed。未知critical255はそのままechoするunsupported。未知optional127を無視して正常にrestartする。

再起動区間ではUART bridge/vendor bulk/TCPの論理3経路でconfirmと同じrestart要求に答えない。
write完了から49/50ms境界をfixture条件として使い、50msでは新bootで全経路がconfirmに答える。
新bootの時計は0、保持者/plan/drive/cacheは空きの状態。旧restartの同じcorr/全byteもno_sessionとなり、古い成功を返さない。
raw制御の要求/返信とwrite/quiet/bootイベントを、通常のChecks traceとは別に保存する。
physicalフレーミング・USB列挙・TCP待ち受け直しの時刻を測るものではない。

`uv run pytest -q tests/test_conformance_restart.py`は27件。
正常1、共通宣言1、wire故障3、状態故障12、再起動後clock成功/boot故障2、制御不足7、fixture遅延条件1。
応答前再起動、再起動欠落、boot再使用、plan/pin/cache/保持者残留、時計の非初期化、
再起動中応答、経路復帰欠落、critical tag書換え、拒否時pin変更を注入して検出する。
失敗時の保留write後始末は元の違反を別のcleanup拒否で隠さない。

targetのhalt/reset状態、保存/未保存設定とfallback、port_speedの起動baud、通知抑止と真の並行経路、
物理再列挙の適合は未証明。reportはfull_conformance=false。
