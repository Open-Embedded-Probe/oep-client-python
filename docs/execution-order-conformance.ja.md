# 資源の副作用から確認するpipeline処理順

`ExecutionOrderChecks`はcore §4.4/9の共通規則を、明示したlogical resource/pipeline fixtureで確認する。
responseの並びと、実際に資源を作成・使用・解放する順序を分けて観測する。

sample fn1/2、kind1/2、max_frame64/window80/max_inflight3、連続allocatorを明示する。
`SampleExecutionOrderAdapter`は既存pipeline adapterへlogical restartと資源/次番号のsnapshotを追加する。
これは実機に追加する公開opやprivate bench依存ではない。

- CLOSE-USE-CREATE：既存IDを解放し、同じIDの使用がno_resourceになり、別fn/kindへ新IDを作る。
- CREATE-CLOSE-USE：次IDを作り、そのIDを解放し、使用がno_resourceになる。元の別資源は保持する。
- 各batchをwriter budget0/1/通常の3状態で実行。停止・部分送信ではresult完了前に実資源とallocatorを観測する。
- resultのcorr/shape/outcome/IDを独立に照合し、再送が元の結果を返して副作用を再実行しないことを確認する。

全case後に所有lockと資源を解放する。
将来IDを予測する部分はfixtureの観測に基づく検査刺激で、通常Hostの資源利用例ではない。
既知の次番号を前提にするため、任意allocator/全失敗holeをこのfixtureの規則へ合わせない。

逆実行後のordered reply、正しいreplyと偽の最終資源、reply順の破損、作成副作用の遅延を検出する。
全op・同時arrival・long execution・物理backend timingは未確認。`full_conformance=false`。
