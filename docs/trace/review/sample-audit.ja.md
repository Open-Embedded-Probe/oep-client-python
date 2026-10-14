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

## 第 2 回の独立レビューの後（乱数の種 4211015、先の 20 個を除く 40 個）

第 2 回のレビュー（docs/trace/review2/*.json。文書の割り振りを変えた別の担当）は、第 1 回が ok とした単位から 80 個の弱い
対応を見つけて直した。その後、新たに無作為の 40 個を同じ方法で読んだ。

| 単位 | 判定 | 根拠 |
|---|---|---|
| core:4.3:13 | ok | 期限切れの後の再送は no_session でなく記録の応答か result_lost |
| transports:4:1 | ok | 8N1・フロー制御なし・115200 で開き、port_speed は速さだけを変える |
| capture:1.1:3 | ok | pos は w 未満で互いに異なり、役割の順 |
| probe-config:2:5 | ok | unset の形と generation |
| transports:3:14 | ok | confirm の前は 64 byte まで、probe は 64 byte の message を受ける |
| probe-config:1:6 | ok | label は firmware の label も線も変えない |
| capture:1.1:5 | ok | サンプル i はビット i·w から（w 1〜128） |
| capture:3.2:23 | ok | 状態表の state 2 の行（各列と自動の遷移） |
| capture:3.2:17 | ok | 使い回された位置は次の読める位置へ gap つき |
| core:4.3:5 | ok | window_exceeded の値（断るのは任意） |
| capture:2.1:4 | ok | trigger は最初の区画だけを確定し、pretrigger を残す |
| fixture:2:17 | ok | TX は plan の間 high、解けば空きの状態 |
| debug:3:16 | ok | reset TLV の形と長さ違いの malformed |
| capture:4.1:11 | ok | track のエラーで組が 6、ほかを stop と同じに止める |
| core:4.3:14 | ok | セッションの判定は見出しの後、payload の前 |
| capture:3.3:13 | ok | trigger の type をインターフェースごとに |
| transports:1:6 | ok | report の count のバイトをつないだものが長さつきの列 |
| fixture:4:14 | ok | status の欄と、上限で止まる数 |
| console:2:10 | ok | DmseqChecks の RESYNC の case（0x41 / 2）と故障注入 |
| core:9:2 | ok | end / 期限切れで資源を解き、順序状態と記録を残す |
| fixture:2:15 | ok | ストリームは plan が作り、plan と configure を解けば消える |
| core:12.1:4 | ok | 変わった固定の形は新しい revision。host は知らない revision を使わない（仕様の書き方の規則の host 側） |
| core:10.2:3 | ok | fn 0 は通知を送らず、host は fn 0 を購読しない |
| capture:1.2:15 | ok | 8 ビットの例の s / o / b |
| core:7.3:1 | ok | first が数以上なら要素 0 個・more 0 |
| capture:1.2:1 | ok | s は 8 / 16 / 32 |
| debug:2:6 | ok | RetryChecks が再試行の前後で target の状態全体を比べる（「retry changed ... target state」） |
| core:10.3:2 | ok | unsubscribe の形とロック |
| core:5.1:9 | ok | 再送に答えが無ければ経路を立て直すまで送らない |
| fixture:3:12 | ok | 書き込み 1 回が 1 フレーム、max_length の切り詰めと errors |
| transports:4:5 | ok | 0x00 COBS 0x00 の候補だけを処理する |
| transports:1:7 | ok | count 0 の report は空 |
| capture:2.1:9 | ok | 送ったデータが読めなければ write_pos・gap 1・len 0 |
| core:7.1:5 | ok | 範囲外の revision は unsupported 0x00 と supported、min > max は malformed |
| console:1:7 | ok | close の形・ロック・必須、閉じた stream は no_resource |
| probe-config:1:4 | ok | channels 以上と firmware の channel は unsupported（項目 tag） |
| core:6.1:3 | ok | 持ち主の処理の間は lease を止め、応答で数え直す |
| capture:1.1:6 | ok | チャネル k はビット i·w + pos[k] |
| plan:2.1:2 | ok | 名指した fn だけを置き換え、断れば何も変えない |
| debug:2:1 | ok | reset の後、DMSTATUS が答えるまで max_op_ms 以内で読み直す |

40 個すべてで、名指された試験が規則を確かめていた（第 1 回の後の抜き取りは 20 個中 1 個が弱かった）。
