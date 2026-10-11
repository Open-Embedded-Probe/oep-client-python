# configのページングとhash変更時の再読込み

ConfigPagingModelとChecksへSPEC registryを明示し、InterfaceChecksの後に
`ConfigPagingChecks(checks, SampleConfigPagingAdapter(model)).run()`を実行する。
privateベンチや隣接SPECを自動探索しない。max_frame 64/128/256の3つの明示profileを使う。
label用channelは512、GPIOの駆動候補は従来の0〜7。大きい番号のlabelを確認するための論理fixtureであり、512本の物理pinを模擬する意味ではない。
RegisteredOps2/context1/5ケース＝8項目、共通宣言33項目をprofileごとに検査する。

- EMPTY：項目なしのfirst 0/1/65535はmore 0の空ページ。
- DENSE：12個のlabelを逆順・criticalでsetし、数値キー順とcritical bitの解除を確認する。channel 255/256/257/300/511を含む。複数ページを欠落・重複なく読み切り、どのページでもhashが同じ。64 byteでは最初の2項目が応答全体64 byteにちょうど収まる。
- FIRST：plan/label/idle/disableの17項目に対し、すべての開始位置0〜17と65535を照合する。firstはbyte数ではなく項目数で、tag順と複数fieldキー順を守る。
- CHANGED：最初のページを返した後にwireのsetでlabelを変更する。次のページのhashが変わったら先頭から読み直し、古いページを混ぜず新しい12項目だけを返す。
- READONLY：live GPIO/planを保持したままSID0でget/stateする。設定、保存、pin、plan、lease、再送cacheを変えない。

期待項目はcheckerのwire刺激から定義し、モデルのcanonical/hash作成関数をoracleに使わない。
ページの項目数はProbeが選ぶため、最大まで詰める一般規則を追加しない。
64 byteのexact fitだけは明示fixtureの境界条件として照合する。
Host部品read_config_pagesは小さい独立例であり、既存production Config.getへ接続した証明ではない。
hash変更時は古い項目を捨ててfirst 0へ戻す。more 1なのに項目が無ければ失敗する。
繰返し変更への最大再試行数はこのHost例の採用方針で、SPECの上限・Probeの動作規則ではない。

新規単体54件：正常3、共通宣言3、wire故障15、意味的故障16、Host故障6、制御不足5、profile不一致2、Host reader拒否4。
byte辞書順/byte offset、項目抜け/重複、誤ったmore/hash、途中で切ったTLV、critical bit残留、frame超過、
読取りの設定/pin/lease/cache変更と、Hostが再読込みせず異なる版を混ぜる故障を検出する。
初期モデルの256 byte固定ページ上限は64/128で応答長違反となり、通知したmax_frameから応答headerを差し引く形へ修正した。

all config items、slot/uart保存参照、任意channelカタログ、真の並行要求/継続変更、production Host連携、
saveの書込み抑止/排他/電源断、物理設備は残る。full_conformance=false。
