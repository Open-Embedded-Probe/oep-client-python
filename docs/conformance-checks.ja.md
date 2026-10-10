# SPEC からの適合検査

仕様を先に定め、適合テストを作り、コアを固めてから実装を追従させる。検査器は指定した SPEC checkout を読み、実装側の旧 registry から期待値を作らない。Python の Host/virtual probe/Arduino firmware は、この検査器を追加しただけでは新仕様に追従しない。

検査結果は次の3段階に分ける。

| 段階 | すべての実装への適用 | 検査する契約 |
|---|---|---|
| core | 全 OEP 端点 | transport、要求/応答、拒否順序、再送、session、時間、資源寿命 |
| interface | 独自拡張を含む全インターフェース | 名前/revision/instance、describe/ops、TLV、共通の資源・購読・ページング規則。各機能で適用されるものを検査 |
| oep-interface | 実装する標準 OEP インターフェース | 各 op の動作・副作用・競合・異常系。独自拡張向けテストの雛形として再利用 |

`oep-conformance` は最初の2段階の一部を実行する。PASS は実行した項目だけの成功で、全体適合ではない。JSON の `full_conformance` は false、`levels` と `unchecked` に範囲を残す。OEP固有の動作検査はまだ実行しない。

## 実行

`tests/equipment/.env.example` をコピーし、明示的に読み込む。設定は安定した port/名指しした USB/TCP、期待する unit ID、SPEC checkout、既存の共有 lock、新規 JSON 出力先。SPEC は隣接 checkout を自動探索しない。私的設備台帳への依存はない。配線・target の指定はこのコア検査には不要。`OEP_CONFORMANCE_FRAMING=serial` では独立した COBS/CRC 検査器を使い、47項目のメッセージ検査に8項目のwire検査と4項目の再接続検査を加える。`tcp` は明示した `tcp://HOST:PORT` に独立したlength検査器を使い、47項目＋7項目のTCP検査を実行する。DNS-SDや隣接設定を自動探索しない。`client` は従来backendを使い、USB/TCPでもメッセージ検査を実行できる。

```sh
uv run --group dev --env-file tests/equipment/.env pytest tests/equipment/test_core_contract.py
# 同じ検査器のCLI入口
uv run --env-file tests/equipment/.env oep-conformance
```

出力先の親ディレクトリを先に用意する。既存の結果を上書きしない。共有 lock を接続から close まで保持し、自分の session を終了する。forceは同じ検査器が取得したsession間の移譲だけを検査する。最初のopenが拒否されたらforceを送らない。別利用者のsessionは奪わない。pin 操作、設定変更、flash は行わない。現行仕様へ未追従の実装も FAIL として記録する。

## 新コアのバーチャルベンチ

既存のCLIに `--profile core-v1` を指定すると、新コア仕様 `4681d22` の最小端点を起動する。既存のtarget付きprofileは固定した旧仕様のまま。このprofileはfn 0のみで、target/fixture/interface/resource/subscriptionを持たない。共通インターフェースやOEP固有opまでPASSしたとは扱わない。

```sh
uv run python -m oep_client.virtual_bench_serve --pty --profile core-v1
# またはTCP。表示されたPORTを明示して使う。
uv run python -m oep_client.virtual_bench_serve --tcp 0 --profile core-v1
```

表示されたportと `OEP_CONFORMANCE_UNIT_ID=virtual-core-1` を明示設定に入れる。serialでは59項目、TCPでは54項目を検査する。複数接続を明示設定したTCPでは60項目になる。必須の `tests/test_virtual_core.py` はraw-message、実際のPTY、loopback TCPで同じ公開検査器を使い、子processを終了まで管理する。通常のテストでは実機を開かない。コアが通った後に、既存profileのinterfaceを新仕様へ順に追従させる。

## 明示したTCP peerとの検査

同時に2接続を受け付ける設備では、`OEP_CONFORMANCE_TCP_PEER=tcp://HOST:PORT`（CLIでは `--tcp-peer`）を追加する。同じlistenerのaddressでよい。別listenerを指定する場合も同じ個体・boot・core/interface宣言を照合する。DNS-SDで候補を探さない。設定がなければ通常の54項目だけを実行し、複数接続は未検査に残す。設定したのに接続できない・個体が違う場合はFAILであり、skipや単一接続へのfallbackにはしない。全TCP実装に同時接続数2を要求するものではない。

追加する6項目はpeerの識別、共通lock/ownerと他sessionの拒否、応答の送り先、接続ごとの途中frameの分離、片側のclose後のlock保持、peerの過大length切断時のprimary保持。応答漏れの無応答観測は100ms、途中header待ちは300ms。JSONにprimary/peerのraw bytesと観測を残す。

close後はsession 0のpeerからlockを観測し、primaryを開き直して再照合する。元のsession Sの要求は新接続へ移さず、自分のownerを確認できた場合だけ別session Tでforceし、Tを同じ新接続でendする。識別・boot・owner・takeoverが不確かなら後続を止め、閉じた接続のsessionの後始末はleaseに任せる。同一sessionのTCP経路移動と再送は未検査として残す。

## raw USBとソフトウェアモデル

bulk/HIDには独立したraw adapter APIと最小USBモデルを追加した。[USB検査ガイド](usb-conformance.ja.md)を参照。モデルでbulk 55項目、ID付きHID 60項目を実行する。USB device/gadgetやOSのUSB stackではなく、実機raw adapterとdescriptor検査は未実装。通常CLIのUSB `framing=client` は従来どおりメッセージ検査のみで、モデルの成功を実機へ適用しない。

## 現在の粒度と限界

| 項目 | 検査 |
|---|---|
| confirm | magic/revision、固定部、frame/window/inflight、transport、boot ID |
| discovery | 個体照合、list の件数/境界/重複、describe のページング/末尾/不変性、ops の形、transport対応、max_op_ms、describe TLVのframe上限、ロック中の発見とconfirm後の履歴 |
| headers/TLV | 結果 role/corr/resolution/detail/長さ、固定部分後の未知応答TLV、拒否優先順、confirmの短さ/magic/revision範囲、要求TLVの短さ/長さ超過/ID 0（critical含む）/未知criticalと非critical |
| session | keepalive、他 session の lock 拒否、lease 下限/上限/既定、ownerの重複と同ID open、自然失効、header拒否とpayload拒否のleaseへの影響、自分が取得したsession間だけのforce |
| replay | 未実行でもhighwater以下のcorrはresult_lost、拒否結果の保持、payloadを直した同corrの拒否、header検査と再送検査の優先順、完全一致結果または result_lost、同 corr の要求変更拒否、同 ID open 後の履歴、end後の再送、open再送による復活禁止、再送によるlease延長禁止 |
| u16 | 半周を越えた corr、session 0 の独立性、corr 65535 の end。番号を一周させない |
| serial wire | 全byte境界での分割、未知role frameと要求の結合、CRC/COBS破損、短い要求/未知roleの無応答、途中切断後の区切りでの回復、過大frameの無応答とgap後の回復。raw送受信、復号結果、観測時間も保存 |
| TCP wire | lengthのbyte分割・結合、length 0、短い要求・未知roleの無応答、frame gapを越える途中待ちでもフレームを保持、過大length時の切断。raw bytesとclose/resetを保存 |
| TCP peer（明示設定時） | 接続前の同一個体照合、共通lock/owner、応答の接続分離、途中headerの分離、primary切断後のlock保持、自分のsessionだけのtakeover、peer切断faultのprimaryからの分離 |
| reconnect（serial） | OS portを閉じて同じportを開き、confirm/個体/bootを再照合。sessionとlock、再送履歴、終了したsessionの履歴、close中のlease失効を検査。再起動や個体の不一致はFAILとして後続を止め、異なる端点へendを送らない |
| interface 共通 | 各 fn を独立に describe/ops/不変性/instance 検査。独自インターフェースにも同じ検査 |

各項目に条項、PASS/FAIL/BLOCKED、要求と応答 hex、経過時間を保存する。wireの復号・読み出し自体が失敗しても、今回のraw送受信とerrorを保存する。過去のwire記録を今回の失敗へ転記しない。timeout/transport切断やcleanup失敗の後は回復を隠さず BLOCKED にし、実行全体は FAIL。設定が全くない任意の pytest 実機入口だけ SKIP。TCPのmax_frameが65535なら、u16でそれ以上のlengthを送れないため過大lengthの項目だけnot_applicableとし、その理由をpytestのskipに残す。設備不備のskipとは区別する。不完全な設定、アクセス権不足、個体違い、仕様違反は FAIL。

`result_lost` は仕様が許す保持サイズ上限を考慮して受ける。履歴の古い clock を送り直したとき、新しい時計値で completed を返せば再実行として失敗する。cacheの容量を実装固有の固定値と決めつけない。

実機bulk/HIDのraw adapter・descriptor・packet終端、同一sessionのTCP経路移動・新接続での再送、force時の資源解放、subscription、電気的なpin解放、複数target・各opの動作は未検査。serialではclientのencoder/decoderを使わず、CRCは標準ライブラリ、COBSと受信は別コードで確認する。OSのport排他とportの開閉だけ既存clientを利用する。再接続はOS portのclose/openであり、USBの物理抜き差し・給電断・USB resetを模擬しない。DTR/RTSやbridge配線により再起動した場合、boot変化のFAILとして記録し、session保持の成否は確定しない。重複応答の検査は結果後30msの観測窓内、過大frameの無応答は宣言されたframe gap＋100msの観測窓内に限る。TCPは独立したlengthの受信・送信を使い、壊れた応答の後は立て直しで隠さず止める。framing=clientでは独立wire検査を実行しない。これらを埋める前に全コア準拠・全インターフェース準拠と名乗らない。

既存 `oep-hardware preflight` は個体・旧仕様の宣言・open/end に限った別契約。portable C++、共有ベクタ、仮想テストの成功も、実機適合とは別に報告する。

既存の registry/vector は `tests/vectors/SPEC_COMMIT` と `SPEC_SHA256` で固定し、通常試験はその snapshot を検査する。明示した `OEP_SPEC_DIR` があれば、同じ commit の upstream 内容も比較する。SPEC の HEAD が先行しても旧回帰は自動同期しない。更新は `OEP_SPEC_DIR=/explicit/path OEP_SPEC_REF=<commit> tools/sync_registry.sh` で行う。

資源寿命には[明示adapterによる補助検査](resource-conformance.ja.md)を追加した。最小サンプルは14資源契約＋2宣言検査を実行する。通常CLIへ自動追加せず、購読・route切断・依存順・電気的idleを未検査として残す。

購読・出来事の寿命には[外部刺激adapterの補助検査](subscription-conformance.ja.md)を追加した。サンプルはidentity＋15動作契約＋2宣言の18項目。raw通知と観測窓を保存する。data、応答優先、route、queue上限は未検査で、通常CLIには自動追加しない。

[data/eventとまとめ送りの補助検査](data-conformance.ja.md)は購読18項目＋data前提検証＋14データ契約の33項目。外部byte位置とloss-freeな刺激条件を明示し、閾値・first-byte delay・共通seq・分割・再送/同session openでの未送信データ保持を検査する。seq一周は単体で65,538個を実際に生成して検証し、実時計・実機の結果と分ける。

[経路・writerの補助検査](route-conformance.ja.md)はinstrumentationを持つ論理モデルで9項目。応答/通知の経路、優先順位、未送信suffixのqueue上限、dropのseq、切断後のsession/資源/購読/履歴保持を確認する。実際のframing/OS buffer、経路別window/max_inflight、並行実行と実機切断は未検査。

[経路別受付と回復の補助検査](pipeline-conformance.ja.md)は明示したreject-overflowモデルで9項目。count/byte境界を分離し、別接続の独立性と応答送信後の回復を確認する。超過拒否はSPEC上任意で、一般実機の必須条件に追加しない。

[混雑時のsession再送・拒否cache](replay-pressure-conformance.ja.md)はpipelineを含む15項目。再送/見出し/session判定を混雑より先に適用し、拒否結果の再送とpeerのsession 0照会が共有履歴を壊さないことを確認する。Sはprimaryに固定し、別ID Tのforceで共有cacheが置換されることも確認する。

[応答送信とlease](lease-conformance.ja.md)はpressureを含む22項目。論理writerの全応答完了で更新し、部分送信/再送/前段の拒否では更新しない。送信待ちでの失効と、end/force後の古い応答での復活・誤更新を検査する。物理的な送信完了は未検査。

[再送cacheの保持上限](retention-conformance.ja.md)は明示した16 byte / 8件のfixtureで11項目。要求/応答それぞれの境界、欠落marker、追い出しと未使用の古いcorr、拒否/end/同session openでの保持を確認する。要求が保持されていれば同一性を先に比較し、変更要求のmalformedを応答欠落のresult_lostと区別する。

[保持欠落とsession/boot寿命](retention-lifecycle-conformance.ja.md)はretentionを含む19項目。期限切れでは履歴を残し、異なるsessionでは置換し、新bootでは履歴/資源/送信待ちを無効にする。明示logical-model resetだけを許し、物理設備の再起動は行わない。

[corr u16の境界](corr-conformance.ja.md)はretentionを含む17項目。通常の大小比較、半周の前後と上限、古い番号の拒否、SID 0の独立性、65535をendに残して別sessionへ切り替える手順を検査する。

[result_lost後の状態読み直し](recovery-conformance.ja.md)はretentionを含む22項目。sample hostがwireだけでboot/sessionと現在の資源を確認し、結果不明の変更要求を新corrで再実行しないことを検査する。元のoutcomeはunknownのままとする。

[回復中のforceと経路単位の結果不明化](recovery-races-conformance.ja.md)は条件ごとに27項目。読み出し前/途中/最後のsession差し替えを検出し、全応答喪失・先頭だけ受信・全件未実行の3条件で、その経路の未解決要求だけをunknownにする。

[corr枯渇前のsession切替](rollover-conformance.ja.md)はcorrを含む23項目。end用番号を残し、未解決要求の処理・boot/reader確認を経て別IDのopen 1へ切り替える。旧資源を無効にし、結果不明の旧操作はunknownのまま保持する。

[end/openの結果喪失からの回復](session-loss-conformance.ja.md)は保持検査を含む21項目。同一要求を再送し、現在のsession状態も確認する。保存されたopenの成功を取得しても、期限切れなら利用を再開しない。result_lostから成功やlease値を推測しない。

[番号枯渇時の複数pending放棄と新経路での回復](exhausted-conformance.ja.md)は保持検査を含む13項目を3条件で実施する。旧Sを移さず、解放をSID 0で確認してから別IDでopenする。旧要求のunknownと受信済み結果を保持する。
