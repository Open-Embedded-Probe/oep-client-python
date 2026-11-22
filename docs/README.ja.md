# docs の文書

[English](README.md)

## 使う

| 文書 | 中身 |
|---|---|
| [使い方ガイド](guide.ja.md) | インストール、つなぎ方、ピンの割り当て、フィクスチャ、キャプチャ、デバッグと書き込み、設定 |
| [`oep` の命令](cli.ja.md) | `oep` の命令、残したセッション、TCP、ピンを探す、UART bridge を速くする |
| [モジュール](reference.ja.md) | 公開の API |
| [SWD のボードを書く・デバッグする](swd.ja.md) | Arduino IDE と gdb から OEP のプローブ越しに |
| [pytest のプラグイン](pytest.ja.md) | pytest-embedded-arduino-cli から OEP のプローブを使う |
| [仮想ベンチ](virtual-bench.ja.md) | ハードウェアなしで動くプローブと target |

## 試験と設備

| 文書 | 中身 |
|---|---|
| [全体テスト方針](testing-policy.ja.md) | OEP と利用プロジェクトの保証対象と責任 |
| [利用プロジェクト向けガイド](testing-consumers.ja.md) | 任意の言語・target・framework で OEP を使う側のテスト手順 |
| [最小構成の動作確認](hardware-quickstart.ja.md) | validate/plan/仮想 smoke と `.env` の実行手順 |
| [構成と配線の確定](hardware-configuration.ja.md) | 個体、探索、確かめた接続、役割、共有制御の設計 |
| [実機設定形式](hardware-schema.ja.md) | 公開のキーと判定、入力と確定 TOML、`.env` の雛形 |
| [Python 実機ハーネス](../tests/hw/README.ja.md) | 設定と手順 |

## 適合検査と仕様

| 文書 | 中身 |
|---|---|
| [SPEC からの適合検査](conformance-checks.ja.md) | core / interface の公開検査器と実行範囲 |
| [適合検査と SPEC の版](conformance-spec-compatibility.ja.md) | 検査器の本文の固定と共有ベクタ |
| [規則ごとの対応表](trace/README.ja.md) | oep-spec の規範の単位と、それを確かめる試験 |
| [仕様追従で見つけた負担](spec-follow-notes.ja.md) | 実装が不便な仕様の規則（見直しの材料） |

検査器ごとの文書:

| 文書 |
|---|
| [資源番号の上限と再起動の検査](allocator-conformance.ja.md) |
| [analog captureの論理操作検査](analog-conformance.ja.md) |
| [アナログ成功payloadと生データの検査](analog-payload-conformance.ja.md) |
| [ARM-ADIの独立検査とlogical fixture](arm-adi-conformance.ja.md) |
| [標準wire attachの事前検査とconsole open](attach-conformance.ja.md) |
| [optional block操作と状態復元の検査](block-conformance.ja.md) |
| [capture-groupの論理操作検査](capture-group-conformance.ja.md) |
| [groupの即時開始と時刻の検査](capture-group-geometry-conformance.ja.md) |
| [captureのdescribe検査](capture-metadata-conformance.ja.md) |
| [capture区画serialの周回とrelease](capture-serial-conformance.ja.md) |
| [異なるレートのgroup取得検査](capture-timeline-conformance.ja.md) |
| [異レートgroupの停止・再送・寿命検査](capture-timeline-lifecycle-conformance.ja.md) |
| [core clockの検査とlogical fixture](clock-conformance.ja.md) |
| [コア大枠検査](core-smoke-conformance.ja.md) |
| [corr u16の境界検査](corr-conformance.ja.md) |
| [線切れの応答と解放の論理検査](cut-conformance.ja.md) |
| [データ通知とまとめ送りの検査](data-conformance.ja.md) |
| [riscv-dmのdmi・halt/resumeとconsole調停の論理検査](debug-conformance.ja.md) |
| [資源を指定するstreamとdebug connectionの寿命検査](dependency-conformance.ja.md) |
| [core発見処理のcursor・channel名検査](discovery-conformance.ja.md) |
| [probe側dmseq framingの検査](dmseq-conformance.ja.md) |
| [target側dmseqの論理検査](dmseq-target-conformance.ja.md) |
| [資源の副作用から確認するpipeline処理順](execution-order-conformance.ja.md) |
| [corr枯渇時の複数pending放棄と新しい経路での回復](exhausted-conformance.ja.md) |
| [fixture系のdescribe metadata検査](fixture-metadata-conformance.ja.md) |
| [GPIOとplanの論理動作検査](gpio-conformance.ja.md) |
| [GPIO planを使ったcore寿命検査](gpio-lifecycle-conformance.ja.md) |
| [コア応答の固定形と既知TLVの読出し検査](host-core-shapes-conformance.ja.md) |
| [Host待受けループの期限開始点](host-deadline-conformance.ja.md) |
| [Hostの通知振り分けと期限の検査](host-notification-conformance.ja.md) |
| [Host要求のcritical指定を検査する](host-request-conformance.ja.md) |
| [同一要求の再送と結果不明後の復帰](host-resend-conformance.ja.md) |
| [HostのTLV読出し部品検査](host-tlv-conformance.ja.md) |
| [Hostの未知値分類](host-value-conformance.ja.md) |
| [Hostのsave待ち時間と転送時間の部分検査](host-wait-conformance.ja.md) |
| [I2C targetの論理検査](i2c-conformance.ja.md) |
| [全インターフェースに使う宣言の準拠検査](interface-conformance.ja.md) |
| [全拡張に使うインターフェース大枠検査](interface-smoke-conformance.ja.md) |
| [応答送信とleaseの補助検査](lease-conformance.ja.md) |
| [同じProbeの複数経路でlinkを検査する](link-routes-conformance.ja.md) |
| [linkの必須source/sinkを検査する](link-transfer-conformance.ja.md) |
| [logic captureの論理検査](logic-conformance.ja.md) |
| [標準consoleのSDI/DMDATA mailbox検査](mailbox-conformance.ja.md) |
| [通知seqの保持範囲](notification-retention-conformance.ja.md) |
| [通知seqの一周・再購読・再送の検査](notification-sequence-conformance.ja.md) |
| [標準wire／console固有の宣言検査](oep-declaration-conformance.ja.md) |
| [OEP固有の代表シナリオ検査](oep-example-smoke-conformance.ja.md) |
| [採用を明示したwrite受付とdebug完了結果の検査](outcome-conformance.ja.md) |
| [ピン宣言の組み合わせを検査する](pin-constraints-conformance.ja.md) |
| [経路ごとの要求受付と回復の補助検査](pipeline-conformance.ja.md) |
| [UART bridgeのport_speedを検査する](port-speed-conformance.ja.md) |
| [Probeのroleと要求見出し](probe-input-conformance.ja.md) |
| [result_lost後の状態読み直しの補助検査](recovery-conformance.ja.md) |
| [回復中のforceと経路単位の結果不明化](recovery-races-conformance.ja.md) |
| [同時に不正な条件がある要求の拒否順序](refusal-order-conformance.ja.md) |
| [登録済みOEPインターフェースのop宣言検査](registered-ops-conformance.ja.md) |
| [混雑時のsession再送・拒否cacheの補助検査](replay-pressure-conformance.ja.md) |
| [optional resetとDM復帰の検査](reset-conformance.ja.md) |
| [資源寿命の適合検査](resource-conformance.ja.md) |
| [応答待ちとforceの競合](response-race-conformance.ja.md) |
| [Probe restartの部分適合検査](restart-conformance.ja.md) |
| [再送cacheの保持上限とresult_lostの補助検査](retention-conformance.ja.md) |
| [保持欠落・履歴の追い出しとsession/boot寿命の補助検査](retention-lifecycle-conformance.ja.md) |
| [線の再試行の論理検査](retry-conformance.ja.md) |
| [通知経路・応答優先・送りかけqueueの検査](route-conformance.ja.md) |
| [optional runとloader状態の検査](run-conformance.ja.md) |
| [標準wireの有限scan検査](scan-conformance.ja.md) |
| [scanのidle_clockとtargetselの検査](scan-options-conformance.ja.md) |
| [選択した標準describe metadataの検査](selected-metadata-conformance.ja.md) |
| [end/openの結果喪失からの回復を検査する](session-loss-conformance.ja.md) |
| [lease・forceの大枠検査](session-smoke-conformance.ja.md) |
| [SPI targetの論理検査](spi-conformance.ja.md) |
| [optional stepと再停止の検査](step-conformance.ja.md) |
| [採用を明示した位置付きストリームのread/marks検査](stream-conformance.ja.md) |
| [購読・出来事の寿命検査](subscription-conformance.ja.md) |
| [TCPフレームの途中休止と再接続](tcp-recovery-conformance.ja.md) |
| [TLV要求の文脈・幅・critical検査](tlv-request-conformance.ja.md) |
| [UARTの論理動作とplan寿命](uart-conformance.ja.md) |
| [plan所有UARTへ適用する共通stream検査](uart-stream-conformance.ja.md) |
| [raw USBの適合検査](usb-conformance.ja.md) |
| [wireのattachオプションの検査](wire-options-conformance.ja.md) |
| [標準資源と応答writerのlease検査](writer-lease-conformance.ja.md) |
| [有限保持の通知出力：代表fixture](notification-output-sample.ja.md) |
| [コアの固定フィールドと省略のレビュー](core-fixed-fields-review.ja.md) |

ここにある文書の保守窓口は oep-client-python です。保証と合否の所有者は各実装に分かれ、通信の規範は oep-spec に置きます。全体方針は
プロジェクト共通で、Python のテストだけの説明ではありません。実設備の台帳は外部で管理し、runner へ明示設定で渡します。
