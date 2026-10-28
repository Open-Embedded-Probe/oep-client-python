# コアの固定フィールドと省略のレビュー

core §2.3の「固定部分に省略できるフィールドを置かない」を、revision1のコアpayloadへ適用する。
本文SHAは`d63f99f1809146ad41f1643526c81417b366d7a9d85af3845daee6654a229a14`（oep-spec 02c08c9、[core形の検査](host-core-shapes-conformance.ja.md)の固定を共有）。
CoreFixedFieldChecksはSPECを明示指定し、本文・番号が変わればレビューし直す。
生成registryは番号の照合に使う。幅・省略条件は本文§1.1/4.3/6.4/7を読んで下の台帳に固定した。
privateベンチへの依存や隣接SPECの自動探索はない。

以下の固定フィールドはすべて必須。意味のあるゼロ値、空の並びもbyteを省略しない。
後続TLVを省略しても固定部分の幅は変わらない。可変の名前にはname_lenを必ず置く。

| op | 必須要求payload | 必須成功payload | 固定prefix幅（要求/成功） |
|---|---|---|---|
| confirm | magic4、min_rev u8、max_rev u8 | magic4、revision u8、max_frame u16、window u32、max_inflight u8、boot_id u32 | 6/16 |
| list | first u16 | total u16、count u8、count個のentry | 2/3＋entry |
| describe | fn u16、first u16 | more u8 | 4/1 |
| clock | 空 | boot_id u32、uptime_ns u64 | 0/12 |
| open | lease_ms u32、force u8 | lease_ms u32、boot_id u32 | 5/8 |
| end | 空 | 空 | 0/0 |
| keepalive | 空 | 空 | 0/0 |
| lock_state | 空 | locked u8、remaining_ms u32 | 0/5 |

list entryはfn u16、instance u16、revision u8、name_len u8、name。
固定prefix6 byteとname_lenで決まるnameが必要で、nameは1〜48 byte。
count0でもtotal/countはある。describeに続く宣言がなくてもmoreはある。
locked0でもremaining_ms0はある。openのlease_ms0は既定を選ぶ値であり、force0も必ず送る。
clockのuptime0やunsupportedのtag0も固定fieldを省略する意味にはしない。

rejectedの固定payloadはreasonが決める（§4.3）。
unknown_function、unknown_operation、malformed、unavailable、window_exceeded、no_session、
session_required、no_resource、result_lostの9種は空。
lockedだけはremaining_ms u32の4 byte、unsupportedはtag u8の1 byteが必須。
unavailableの詳細は任意TLVであり、固定cause byteを付け足さない。
locked/lock_stateのownerは、設定した場合だけ存在するTLV。ownerなしでも固定部分は同じ。

検査はCoreFixedFieldChecksを、採用する結果decoderと、独立な論理Probe Checksを返すfactoryに対して実行する。
`CoreFixedFieldChecks(decoder, probe_factory, spec="/explicit/oep-spec").run()`。
公開結果reader部品`conformance_host_core_sample.decode`とResourceModelのCoreを参照例として使える。
factoryは検査用の独立状態を毎回返す。実機での無断session操作を行うためのAPIではない。
REVIEW、REQUEST-FIELDS、RESPONSE-FIELDS、ZERO-VALUES、OWNER-TLVを検査する。
各要求/応答の固定fieldを丸ごと抜いた刺激と、list要素の各field欠落を確認する。
これはtailを含まない欠落刺激であり、任意byte列を足した全曖昧性を証明しない。

`uv run pytest -q tests/test_conformance_core_fixed_fields.py`は17件。
正常1、応答欠落受理の故障6、短い要求受理の故障4、ゼロ値/owner省略の混同5、旧confirm flags台帳の検出1。
コアpayloadのレビューであり、全message header、拡張の要求/応答/event/data/TLV、
config項目の固定形・省略条件は別途レビューが必要。full_conformance=false。
