# 最小構成の動作確認

設備 TOML の offline 検査・構造上の役割解決、OEP メッセージを使う仮想 smoke、共通ロック付き実機 preflight が使用できます。明示 preflight は実機 probe の単体確認に対応します。target 書込み・配線探索・USB 通信契約は未対応です。Python 3.10 以降と uv を使います。

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
uv run --project ../.. --locked --env-file .env.virtual pytest ../equipment -q
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

次の段階は、target の確定配線と必要能力を照合する adapter、独立診断と複数 connection への対応です。

## 実機 probe の単体確認

公開雛形をコピーし、`example = false`、実際の OEP unit ID と serial/TCP/USB transport を記入した resolved TOML を用意します。probe 宣言だけの確認なら targets/配線の表は不要です。共通ロックは設備管理者が指定する既存ファイルを使います。`.env.example` をコピーし、設定・結果・ロックの path を設定します。

```sh
uv run --locked --env-file tests/hw/.env oep-hardware preflight
uv run --locked --env-file tests/hw/.env pytest tests/equipment -q
```

相対 path は実行時 CWD 基準なので、この例は repository root 基準で `.env` を記入します。結果は `OEP_HW_RESULTS` に自動採番します。`--config`、`--lock`、`--out` でも明示指定できます。通常の pytest は `OEP_HW_CONFIG` がなければ equipment 契約を skip し、実機を開きません。仮想構成を渡すと同じ pytest 入口が smoke を実行します。

preflight は個体・使用版・宣言・session の開閉のみを確認します。target 操作や設定保存は行いません。既存 firmware を使用し、数値的な最低版は要求しません。個体不一致、宣言欠落、session 取得失敗、cleanup 失敗は結果に残して失敗します。例示構成、手動ラベルしかない probe、ロック競合は操作前に拒否します。結果ファイルを予約できない場合も port を開きません。

`preflight` の成功は後で別プロセスが始める target 試験の排他や適合を保証しません。target 試験の adapter は、自分の占有区間で必要な preflight と試験をまとめて実行する必要があります。
