# 貢献の手引き

[English](CONTRIBUTING.md)

## 試験

```sh
uv run pytest                                                            # 仮想ベンチ（実機なし）
OEP_HW_BOARDS=<board id> OEP_HW_NOFLASH=1 uv run pytest tests/hw -m hw   # 実機: tests/hw/README.ja.md
```

最小構成の確認は [動作確認手順](docs/hardware-quickstart.ja.md)。共通の [テスト方針](docs/testing-policy.ja.md) と
[OEP を使うプロジェクトのガイド](docs/testing-consumers.ja.md) も見ます。設備の設定は [構成設計](docs/hardware-configuration.ja.md) と
[設定形式・雛形](docs/hardware-schema.ja.md) にあります。仕様からの適合検査は [SPEC からの適合検査](docs/conformance-checks.ja.md) にあります。

## 番号の表と共有ベクタ

番号は oep-spec の `generated/oep-v1/oep_v1_registry.py` をそのまま写した `oep_client.registry` から取ります。番号の表と oep-spec の
試験ベクタ（`tests/vectors/*.json`。`tests/test_spec_vectors.py` がこのクライアントと仮想ベンチを突き合わせる）は、名指した oep-spec の
checkout から `OEP_SPEC_DIR=<checkout> tools/sync_registry.sh` で写します（`OEP_SPEC_REF` で HEAD 以外の commit を選べます。commit と
ファイルの hash は `tests/vectors/SPEC_COMMIT` / `SPEC_SHA256` に残ります）。

## 文書

文書は日本語（`.ja.md`）と英語（`.md`）を同じ変更で直します。oep-spec の凍結（`v1.0.0`）までは日本語が正です。文書は今の状態だけを
書き、変更の履歴は CHANGELOG.md に書きます。

## リリース

リリースは GitHub Actions の Release（workflow_dispatch）です。version_level（patch / minor / major、既定 patch）を選ぶと、
`tools/prepare_release.py` が pyproject.toml の今の版からその桁を上げた版にします（version に X.Y.Z か X.Y.ZbN を書けば、その版）。
pyproject.toml、uv.lock、`oep_client.__version__` を書き換え、CHANGELOG.md の Unreleased をその版にし、試験と build の後に commit と
tag、GitHub Release、PyPI（Trusted Publishing）へ出します。変更は CHANGELOG.md の Unreleased に (EN) / (JA) で書き足しておきます。
