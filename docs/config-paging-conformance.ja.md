# configのページングとgeneration変更時の再読込み

ConfigPagingModelとChecksへSPEC registryを明示し、InterfaceChecksの後に
`ConfigPagingChecks(checks, SampleConfigPagingAdapter(model)).run()`を実行する。
privateベンチや隣接SPECを自動探索しない。max_frame 64/128/256の3つの明示profileを使う。
fn 0のchannelsは512、GPIOの駆動候補は従来の0〜7。大きい番号の項目を確認するための論理fixtureであり、512本の物理pinを模擬する意味ではない。

getの応答は`more(u8) generation(u32) count(u8) count×項目 [TLV]`。項目はcountで数えて読み、残りは応答の末尾TLVとして別に読む。

- EMPTY：項目なしのfirst 0/1/65535はmore 0/count 0の空ページで、generationは起動時の1。
- DENSE：12個のlabelを逆順・bit7付きでsetし、1項目ごとにgenerationが1進むこと、数値キー順とbit7 0を確認する。channel 255/256/257/300/511を含む。複数ページを欠落・重複なく読み切り、どのページでもgenerationが同じ。64 byteでは最初の2項目が応答全体64 byteにちょうど収まる。
- FIRST：label/idle/disableの17項目に対し、すべての開始位置0〜17と65535を照合する。firstはbyte数ではなく項目数で、tag順と数値channel順（idle 1/256、disable 6/7/511）を守る。
- CHANGED：最初のページを返した後にwireのsetでlabelを変更する。次のページのgenerationが変わったら先頭から読み直し、古いページを混ぜず新しい12項目だけを返す。
- READONLY：live GPIO/planを保持したままSID0でget/stateする。設定、保存、pin、plan、lease、再送cacheを変えない。

期待項目はcheckerのwire刺激から定義し、モデルの並べ替え関数をoracleに使わない。
ページの項目数はProbeが選ぶため、最大まで詰める一般規則を追加しない。
64 byteのexact fitだけは明示fixtureの境界条件として照合する。
Host部品read_config_pagesは小さい独立例であり、既存production Config.getへ接続した証明ではない。
generation変更時は古い項目を捨ててfirst 0へ戻す。more 1なのに項目が無ければ失敗する。
繰返し変更への最大再試行数はこのHost例の採用方針で、SPECの上限・Probeの動作規則ではない。

新規単体61件：正常3、共通宣言3、wire故障15、意味的故障19、応答TLV2、Host故障6、制御不足6、profile不一致2、Host reader拒否5。
byte辞書順/byte offset、項目抜け/重複、誤ったmore/generation、途中で切った項目、bit7残留、frame超過、
countと項目数の不一致、変更ごとに進まないgeneration、読取りの設定/pin/lease/cache変更と、
Hostが再読込みせず異なる版を混ぜる故障を検出する。応答の未知の非critical末尾TLVは項目と混ぜずに受け入れる。
初期モデルの256 byte固定ページ上限は64/128で応答長違反となり、通知したmax_frameから応答headerを差し引く形へ修正した。

任意channelカタログ、真の並行要求/継続変更、production Host連携は残る。
saveの書込み抑止は[別の部分検査](config-storage-conformance.ja.md)で確認する。
saveの排他/電源断、物理設備は残る。full_conformance=false。
