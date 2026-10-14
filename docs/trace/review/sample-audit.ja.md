# 規則の対応表の抜き取り照合（独立レビューの後）

独立レビュー（docs/trace/review/*.json、902 単位すべて ok）の後、除外でない単位から無作為に 20 個（乱数の種 20261015）を選び、
単位の本文と、名指された試験の本体を読んで、その規則を確かめているかを判定した。

| 単位 | 判定 | 根拠 |
|---|---|---|
| transports:1:2 | ok | COBS + CRC-16 LE、前後の 0x00、空フレームの読み飛ばしを試験が直接比べる |
| core:12:12 | ok | 知らない cause は生の値、知らない resolution / outcome は失敗（2 つの試験の合わせ） |
| debug:1:16 | 弱い → 直した | 名指された試験は「見つかった」の判定だけで、scan の書き込みを見ていなかった。`test_scan_leaves_the_target_as_it_was` を足した |
| debug:4:5 | ok | step の後始末を StepChecks が確かめ、その故障注入がある |
| core:2.1:2 | ok | C0 制御文字・0x7F・不正な UTF-8 の置き換えを、owner と locked の表示で比べる |
| core:2.5:6 | ok | 本体の cause は 0x01〜0x3F、インターフェース固有は 0x40〜0x7F |
| core:2.4:1 | ok | 知らない role と要求のフレームを捨て、結果だけを corr で照合する |
| probe-config:2:4 | ok | erase の後の state が保存なし |
| core:12:5 | ok | 各インターフェースの TLV の文脈が op / 出来事 / describe に属する |
| debug:4.6:1 | ok | 保留中の havereset を attach が確認応答し、flags bit0 で示す |
| fixture:4:13 | ok | CS で終わる転送の MOSI と bits、transactions の加算、長さ超過 |
| fixture:3:5 | ok | stretch の番号、ロックの要否、空の応答 |
| console:2:7 | ok | restart の detail 1 / 2 を DmseqChecks と ResetChecks が確かめ、故障注入がある |
| capture:1.2:2 | ok | s / o / b の範囲と order を応答から比べる |
| capture:2.2:10 | ok | 区画の flags bit1（stop）、bit0（再開）、bit2（遅れ）を 3 つの試験が比べる |
| probe-config:4:1 | ok | storage の値と、保存を持たない probe の save / erase / state の不在 |
| fixture:3:2 | ok | read_rx の pending count data と ns TLV |
| transports:2:3 | ok | 書き込みはフレームをまとめて 1 回 |
| debug:4.1:4 | ok | dmi の待ちの step の引数と、値を返さないこと |
| capture:2.2:8 | ok | start_uncertainty_ns の値 |

20 個のうち 19 個は名指された試験が規則を確かめていた。1 個（debug:1:16）はレビューが ok としたが根拠が弱く、試験を足した。
