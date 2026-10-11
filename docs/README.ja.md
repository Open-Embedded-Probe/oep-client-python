# テスト方針と実機基盤の文書

| 文書 | 対象と状態 |
|---|---|
| [最小構成の動作確認](hardware-quickstart.ja.md) | 現在動く validate/plan/仮想 smoke と `.env` の実行手順 |
| [SPECからの適合検査](conformance-checks.ja.md) | core / interfaceの公開検査器と実行範囲 |
| [Host TLVの読出し検査](host-tlv-conformance.ja.md) | 明示readerアダプタで未知tag・critical・重複・破損と長さ境界を確認 |
| [Host通知と期限](host-notification-conformance.ja.md) | 現SPECのrole振り分け、未知kindのseq、通知が続く時の応答待ち/pumpを確認 |
| [Hostの未知値分類](host-value-conformance.ja.md) | 失敗となる未知値、値を保つcause/scan kind、予約confirm flagsを確認 |
| [TLV要求の文脈と幅](tlv-request-conformance.ja.md) | 4つのfn/op文脈、known criticalの同等性、拒否前の非変更と順序を確認 |
| [core clockの検査](clock-conformance.ja.md) | 正確な論理ns時計で採時・SID 0・再送例外・lease・u64境界を確認 |
| [core発見処理の検査](discovery-conformance.ja.md) | list/fn 0 describeの任意cursor、labelと宣言の不変性を確認 |
| [資源番号の上限](allocator-conformance.ja.md) | 明示seedでu16上限、解放後の非再利用、再送と再起動を確認 |
| [通知seqと再購読](notification-sequence-conformance.ja.md) | 論理経路でseqの一周・drop・購読置換・再送を確認 |
| [raw USB検査](usb-conformance.ja.md) | bulk/HIDの独立検査器と最小ソフトウェアモデル。実機raw adapterは未実装 |
| [全体テスト方針](testing-policy.ja.md) | OEP と利用プロジェクトの保証対象、責任、改修計画 |
| [利用プロジェクト向けガイド](testing-consumers.ja.md) | 任意の言語・target・framework で OEP を使う側のテスト手順 |
| [構成と配線の確定](hardware-configuration.ja.md) | 個体、探索、確認済み接続、役割、共有制御の設計 |
| [実機設定形式](hardware-schema.ja.md) | 公開のキーと判定、入力と確定 TOML、`.env` の雛形。offline 初期実装あり、実機連携は未実装 |
| [現行 Python 実機ハーネス](../tests/hw/README.ja.md) | 現在使用できる設定と手順、新形式への移行事項 |

ここにある文書の保守窓口は oep-client-python です。保証と合否の所有者は各実装に分かれ、通信の規範は oep-spec に置きます。全体方針はプロジェクト共通で、Python のテストだけの説明ではありません。実設備の台帳は外部で管理し、runner へ明示設定で渡します。
