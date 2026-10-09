# テスト方針と実機基盤の文書

| 文書 | 対象と状態 |
|---|---|
| [最小構成の動作確認](hardware-quickstart.ja.md) | 現在動く validate/plan/仮想 smoke と `.env` の実行手順 |
| [全体テスト方針](testing-policy.ja.md) | OEP と利用プロジェクトの保証対象、責任、改修計画 |
| [利用プロジェクト向けガイド](testing-consumers.ja.md) | 任意の言語・target・framework で OEP を使う側のテスト手順 |
| [構成と配線の確定](hardware-configuration.ja.md) | 個体、探索、確認済み接続、役割、共有制御の設計 |
| [実機設定形式](hardware-schema.ja.md) | 公開のキーと判定、入力と確定 TOML、`.env` の雛形。offline 初期実装あり、実機連携は未実装 |
| [現行 Python 実機ハーネス](../tests/hw/README.ja.md) | 現在使用できる設定と手順、新形式への移行事項 |

ここにある文書の保守窓口は oep-client-python です。保証と合否の所有者は各実装に分かれ、通信の規範は oep-spec に置きます。全体方針はプロジェクト共通で、Python のテストだけの説明ではありません。実設備の台帳は外部で管理し、runner へ明示設定で渡します。
