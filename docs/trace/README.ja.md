# oep-spec a09fa45 の規則ごとの対応表

`tools/spec_units.py` が oep-spec の core / transports / 各インターフェース文書から規範の単位（箇条、番号つきの手順、表の
データ行）を節ごとに抜き出し、`<文書>:<節>:<番号>`（見出しより前は `pre`）の ID を振る。`<文書>.json` は単位ごとに、
それを確かめる試験（`verify`）と、違反を見破る故障注入の試験（`mutants`）の pytest の ID、または除外の理由（`excluded`:
情報だけの行、参照の一覧、このリポジトリが模せない電気的な線や target 側の規則など）を持つ。

```sh
.venv/bin/python tools/check_trace.py ../oep-spec          # 全文書: 漏れ・ずれ・試験の有無と合否を確かめる
```

`check_trace.py` は、仕様のすべての単位が表にあること、本文の先頭が一致すること、除外でない単位に試験があること、
挙げた試験が集められて通ることを確かめ、どれかが欠ければ PROBLEM を出して失敗する。
