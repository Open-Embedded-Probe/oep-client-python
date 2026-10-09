# 最小構成の動作確認

現時点で使用できるのは、設備 TOML の offline 検査・構造上の役割解決と、実際の OEP メッセージを使う仮想 smoke です。実機への接続・書込み・配線探索・USB 通信は実行しません。Python 3.10 以降と uv を使います。

repository root から、同梱の設定だけで確認できます。

```sh
uv sync --locked
uv run --locked oep-hardware validate --config tests/hw/hardware.example.toml
uv run --locked oep-hardware plan --config tests/hw/hardware.resolved.example.toml --contract USB-DATA
uv run --locked oep-hardware smoke --config tests/fixtures/hardware.virtual.toml
uv run --locked pytest tests/test_hardware.py -q
```

USB の plan は、同一 probe の2 target に `dut` / `peer` を割り当て、2 connection を要求する計画を表示します。これは現行 probe の同時能力や対象の USB API を認定するものではありません。未接続 pair を全 target の直積から作りません。

仮想 smoke は confirm、list/describe、unit ID、必須宣言、session open/end を確認し、申告 firmware と宣言を JSON に残します。通信はプロセス内の COBS/CRC フレーム経由です。結果の `scope` は限定した smoke 範囲を示し、実機適合や USB 通信の保証と区別します。

## .env から指定する

```sh
cd tests/hw
cp .env.virtual.example .env.virtual
uv run --project ../.. --locked --env-file .env.virtual oep-hardware smoke
```

`.env.virtual` は Git 管理外です。uv が明示的に読み込み、CLI は自動探索しません。相対 config path は実行時の CWD 基準です。CLI の `--config` で同じ項目を指定した場合は CLI が優先します。

## 結果を保存する

```sh
mkdir -p .pytest-results
uv run --project ../.. --locked --env-file .env.virtual oep-hardware smoke \
  --out .pytest-results/virtual-smoke-1.json
```

出力先の親ディレクトリは事前に用意します。同じ名前があればエラーにし、過去の結果を上書きしません。失敗した smoke も JSON に保存し終了コード1、設定・入出力エラーは終了コード2です。plan の設備不足は `equipment-unavailable` と表示し、リリース gate の成功とは扱いません。

`--probe` は対象 probe を絞ります。plan は `--target` / `--link` も契約に応じて使えます。明示した対象が存在しない、割当が成立しない場合は設定エラーです。通常の仮想 smoke はすべての選択 probe が virtual transport であることを操作前に検査し、実 transport を一台でも含むと拒否します。

次の段階は、実機の identity・宣言を照合する preflight と、host 共通ロック・既存 runner への adapter です。設定を読み込むだけで実機を操作する入口は作りません。
