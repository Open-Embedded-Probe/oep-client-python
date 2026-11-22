# 仮想ベンチ（動く spec）

[English](virtual-bench.md)

仮想ベンチは、probe とその先の target、治具の配線を、実際の治具に合わせてソフトウェアで作ったもので、ハードウェアなしで host の
ソフトウェアを試す環境である（この repo で「ベンチ」とだけ書けば実機の HIL の治具を指し、ソフトウェアのほうは常に「仮想ベンチ」と書く）。

その probe、`endpoint.Endpoint` は実装する仕様の規範どおりに答え（応答のデータと並びは長さを持ち、どの応答にも TLV が
続けられる、describe は宣言だけで状態はロック不要の状態の op、キャプチャの世代）、ch32rv・この client・probe の firmware を突き合わせる
「動く spec」として使う（spec が変わったら、probe の firmware より先にここを合わせる）。`virtual_bench` は宣言の例（profile:
`p4-x035`、`esp32-v003` = classic ESP32 の firmware と同じフレームの上限 - max_frame 512、riscv-dm の max_length 488 -、
`esp32-v003-64` = 同じ probe を宣言できる最小の max_frame 64 で（max_length 40、list / describe は分けて読む）、
`p4-bench` = RVSWD の場所 3 か所で同時に 2 つの connection を持つ架空の治具、`rp2350-pins` = host がピンを選ぶ wire、`core-v1` = 本体だけで
max_frame 64、インターフェースなし（`virtual_bench_serve` だけ）。probe が自分で持ち宣言しないもの - 自分の channel、コンソールの
256 byte の送りの列、キャプチャの幅・区画の記録・max_read、capture-group の budget - は `Offered.inner` と `VirtualProbe.own_channels`
に置く）、`virtual_bench_serial` はシリアルの口のバイトの側（COBS のフレームだけ。フレームでないものは捨てる）。

`virtual_bench_capture` は `oep.fixture.logic`（ロジック）: ワンショット、リピート（実際のレートで時計どおりに区画ができ、リング、release）、
ストリーミング（購読している間のデータの push。どれも世代と区画を持つ）、レベル / エッジのトリガとプリトリガ、出来事。取れるものは
決まっている: サンプル i はカウンタの値 i で、チャネル k はそのビット k（周期 2^(k+1) サンプルの方形波）。layout は profile が作れる幅
（自分のもので宣言しない。`p4-x035`: P4 の PARLIO と同じ w 1〜16、3 本なら w 4。`esp32-v003`: classic ESP32 の sampler と同じ w 8、
ワンショットだけ）。capture は聞くだけなので、ほかのインターフェースが持つピンにも plan できる。`p4-x035` には `oep.fixture.analog`
（P4 の ADC1 の 4 チャネル、GPIO16〜23。偶数のチャネル k は方形波、奇数は正弦波で、周期は 64 (k // 2 + 1) サンプル。16 ビットの枠に
12 ビットの値。950 / 1250 / 1750 / 3100 mV の 4 つの frontend、作り物の 2 点の較正と Vrefint）と、ロジックとアナログを束ねる
`oep.fixture.capture-group` もある（一緒に始まり、アナログは 5 µs 遅れ（±2 µs））。時刻は probe の 1 本の時計の ns。

`oep.fixture.i2c-target` と `oep.fixture.spi-target`（`p4-x035`、`esp32-v003`）は plan で役割を受け（SDA / SCL、SCK / MOSI / MISO / CS。
`esp32-v003` の SPI は 2 つの channel_group のどれかに完全に一致）、fixture §3 / §4 の op にすべて答える。バスの controller は無い:
試験が endpoint の hook（`i2c_write` / `i2c_read` / `spi_transfer`: バス上の 1 回のトランザクション）を呼ぶまで、何も受けない（spi の arm は待ったまま）。

従う規則の要点。本体: fn 0 は名前を持たず list に載らず、ops は本体の必須のものだけ。list は first だけ。断るのは見出し（corr 0 は
malformed、unknown_function、unknown_operation、session_required）、送り直し（core §5.1: 最後に開いたセッションの最後の corr と、要求全体と
応答の上限つきの記録 - 覚えた corr で中身の違う要求は malformed、覚えていない corr は 1〜32767 先なら新しい要求、ほかは result_lost）、
セッション（no_session、locked と残りの ms と owner）の順で、その後はどれも何も変える前に確かめ、当たった理由のどれか 1 つで答える。
知らない非 critical の要求の TLV は無視し、知らない critical のものは受け取ったままの tag で unsupported、実装する TLV は bit 7 によらず
同じに確かめ、TLV の番号 0 は malformed。unavailable は cause、channel、fn を運ぶ。connection と stream は 1 から始まる 1 つの u32 の番号の
空間を分け合い、同じ boot_id の間は使い回さない。通知（core §10）はその fn の subscribe が来た接続へ送り、出来事はすぐ、データは
min_bytes / max_delay_ms でまとめ、置き換えた・解いた購読のまだ送っていないものは捨てる。時計は起動からの ns（`now_ns=`。
virtual_bench_serve は `time.monotonic_ns` で、boot_id も引く）で、`reboot()` で 0 から。debug: attach は method、ピン、max_speed を固定部分で
名指し、いつも新しい connection を作る（使用中のピンは unavailable cause 1、max_connections を超えれば cause 2）。method 1 は features の
attach_halt があるときだけ。attach、scan、riscv-dm の reset は max_op_ms（10000）のうちに答える（reset の後に自分で再起動する target -
`VirtualTarget.restart_ms` - を待ち、`settle_log` に残す）。reset は status、flags、pc で答える。timeout_ms 0 の run はすぐ止める。準備の
失敗（`VirtualTarget.fail_regs`）と走っている hart は stopped 3 not_run。やり取りに答えの無い target の op は status line で失敗し、線を
駆動しない（`pin_state` は `wire-free`）。後始末に失敗した高水準の op（`VirtualTarget.restore_fails`、`step_stuck`）は答えてから
connection とそのストリームを閉じる。見つかった = DMSTATUS.version が 2 以上で 15 でない（`VirtualTarget.version`）。`halt_stuck` は halt を
時間切れにする。コンソール: connection ごとにセッションが持つストリーム 1 つ。共通のマークのほかに reset 0x40（reset op の ndmreset）と
restart 0x41。DMDATA / dmseq のストリームの送りの列は poll ごとに 3 / 2 byte ずつ target に渡し、poll は hart が走っている間 1 ms に
1 回、止まっている間とその connection の riscv-dm の要求の間はしない（`console_take(sid)` は全部渡す）。fixture: gpio の mode は 0〜6
（外の線は `gpio_world`）。uart は configure するまで何も受けず、status は baud と format。i2c-target は 1 つの形（データのある書き込み
1 回が 1 フレームで max_length で切る。読み出しは preload_tx の置き場から、無ければ 0xFF）。spi-target に reset は無い。capture:
configure の TLV は core §2.3 だけに従う。bind されたトラックの start / configure は unavailable cause 4、資源の足りない bind は cause 2 と
fn。書き込み位置以降の read は write_pos と len 0 を答える。設定（core §13）: 各 fn の項目は describe から、値は出どころを持たない（profile の初期値は保存の無い起動で入る。label は
ASCII の大小文字を区別せずに一意。`changeable=False` で get だけの probe）。長さが形と違う値は malformed。generation は起動から 1 で数える変更の回数。state は storage_state、saved_generation、reason。
起動時は保存した値（カテゴリと項目の名前で）を 1 つずつ確かめ、disable を先に当て、当てられないものは外す（部分適用）。port_speed: 要求が来た UART bridge の口への
baud / step / verify_ms で、port_speed_tolerance_pct の内、phase 0〜3 は port_state で読める。仮想ベンチが戻るのは verify_ms の後、正常な
フレームの無い port_speed_idle_ms の後、セッションの終わり - 壊れたフレームでは戻らない。

試験の hook はほかに: probe がピンに何をしているかは `pin_state(ch)`（MISO は CS が有効な間だけ駆動 - `spi_select` -、i2c-target は
オープンドレインで自分の pull-up だけ - `virtual_bench.with_i2c_pullups` -、plan を取っても使い始めるまでピンは変わらない、閉じた
connection のピンは idle に戻る）。channel に無い pull の idle（`no_pull`）。`virtual_bench.with_unit_id(probe, "x-...")` は `x-` の unit_id
の probe を作り、`virtual_bench.without(probe, name)` はインターフェースを外す（`virtual_bench.without(p, virtual_bench.RESTART)`）。どの fn の
describe も `ops` tag を持つ（profile が書かなければ `VirtualProbe` がそのインターフェースの表の op をすべて立て、
`virtual_bench.ops_of(name, *without)` で任意の op を外す。core §7.4 を破る ops - 試験が `fill_ops=False` で与えたもの - は与えたとおりに
出す）。`core-v1` 以外のどの profile も、前からあった fn の後ろに `oep.probe.plan`（plan_roles 32）と `oep.probe.restart`（restart_max_ms
2000）を並べる: p4-x035 は plan 14 / restart 15、esp32-v003 は 11 / 12、p4-bench は 8 / 9、rp2350-pins は 7 / 8。
`tests/test_spec_vectors.py` は oep-spec のベクタ（sessions.json を 1 段ずつ、ops の場合を byte のとおりに）を仮想ベンチとこの client で走らせる。

外のプログラムの試験には `virtual_bench_serve` を子プロセスで使う:

```sh
uv run python -m oep_client.virtual_bench_serve --pty --profile p4-bench --console 'uptime %d\r\n' --every 100
# 最初の行: PTY /dev/pts/N（--tcp 0 なら PORT n）。stdin を閉じると終わる（--keep-on-eof なら終わらない）
```

stdin に `reboot` の 1 行を書くと、セッションの途中で probe が新しい乱数の boot_id で再起動する（`Endpoint.reboot`）。
セッションの表、送り直しの表、接続、ストリーム、購読、plan、保存していない設定は消え、保存した設定（`--label`）がもう一度当たり、時計は 0 から、シリアルの口は起動時の速さに戻る。古いセッションでの要求は no_session になり、confirm と open は新しい boot_id を返す。
pty や TCP の接続は開いたまま。stderr に `virtual_bench_serve: rebooted, boot_id 0x........` を出す。`lose [CONNECTION]` はその connection（無ければ生きているすべて）の線をずっと失わせる（`Endpoint.lose`、debug §2）: connection とそのコンソールのストリームが閉じ、以後それを名指す要求は no_resource。stderr に `virtual_bench_serve: lost connection(s) ...` を出す。`capture-overflow FN [ERROR]` はキャプチャ FN に、取っている区画の途中のデータを落とさせる（`Endpoint.capture_overflow`）: その区画は出さず、トラックはエラーで止まる（state 6、error は ERROR、既定 2）。ほかの行は stderr に一言出して無視する。oep.probe.restart の restart（oep-if-restart。`core-v1` 以外のどの profile もこのインターフェースを並べ、describe に restart_max_ms 2000 を出す。`--no-restart` はそれを持たない probe にする: list に無く、その fn は unknown_function）は要求で同じことをする: 応答を先に送り、それから probe が再起動し、要求の後ろに読んでいたものは捨てる:

```python
# 試験は子プロセスを stdin=PIPE で持ち、その行を書く
proc.stdin.write(b"reboot\n"); proc.stdin.flush()
```

pty がシリアルの口（host が TIOCEXCL を掛けて開く）、`--tcp PORT` は既定で probe の TCP の経路と同じ length のフレームを話す（`--framing length`:
待ち受けは probe の TCP の経路として describe に載り、confirm がその番号を返す。max_frame を超える長さは接続を閉じる）。
`--framing cobs` は指定したときだけで、socket の上でシリアルの口をまねる（COBS のフレームだけ、一度に 1 つの接続）。length の framing の TCP は同時に `--tcp-connections N` 個（既定 3。参照の probe と同じ。それを超える接続は受けてすぐ閉じる）の接続に答える。どの接続も別の経路（transports §1: confirm と使っている revision は接続ごと、通知はその fn の subscribe が来た接続へ）で、probe は 1 つ: ある接続のセッションがロックを持つ間、別の接続の open は locked で断られ、lock_state はどの接続からも同じに見える。閉じた接続はセッションを終えない（transports §3）: セッション、ロック、購読、送り直しの表は lease が切れるまで残り、閉じた接続への通知は捨て、別の接続からの同じ id の open がセッションを取り戻し、通知もその接続に移る（core §6.2）。`--once` は、接続を 1 つ受けた後に接続が 1 つも無くなったら終わる（length の framing では、重なった接続の最後が閉じたとき）。故障の注入は `--drop N`（N 番目の答えを 1 回出さない。要求は実行済みなので送り直しは覚えた答えを
受ける）、`--noise TEXT`（答えの前に雑音）、`--corrupt N`（N 番目の答えの CRC を 1 回壊す）。`--uart-rx` は、セッションが plan して
configure した最初の fixture UART の RX にバイトを渡し、`--run-hook` は riscv-dm の run を host 自身の模型にする。`--capture-slipped` は capture の
区画すべてに flags bit2 を立てる。
`--silent-until-reset N` は N 番目のピンの組の target を、その線でリセットされるまで（host の reset TLV 付きの attach）何も答えない
ようにする（`--label CH=TEXT`、例 `23=v003.nrst` は host のプロファイル v003 の線を名付ける、host ガイド §5.3）。port_speed: `esp32-v003` の `oep.probe.link` は持つ（`--no-port-speed` で ops から外す）。
`--broken-rate RATE[:MIN_SIZE][:in|out]` はその速さでフレームを壊す（同じプロセスの `virtual_bench_serial.VirtualSerialStream` は、host の速さが
probe の速さと違う間、両方向のバイトをすべて壊す）。出来事とデータの push は pty にも TCP（両方の framing）にも出る。ほかは `--help`。

**自分を広告する（DNS-SD、1 台の上の CI）。** `--announce`（`--tcp` と。length の framing）は、TCP で待ち受ける probe と同じく
`_oep._tcp` の mDNS / DNS-SD の問い合わせに答える（transports §3）: PTR `_oep._tcp.local.` -> instance `OEP virtual <unit_id> <port>`、
その SRV（port と host `oep-virtual-<unit_id>-<port>.local.`）、その TXT `unit_id=<unit_id>`（fn 0 の describe のもの。`--unit-id ID`
で決める）、host の A の record: `--listen` のアドレス（既定 127.0.0.1。`--listen 0.0.0.0` なら広告するインターフェースすべてのアドレス、
loopback は最後）。`mdns` の extra があれば python-zeroconf が答え、無ければこのパッケージの最小の応答器（`virtual_bench_mdns`。IPv4）:
5353 以外の port からの問い合わせ（legacy unicast、RFC 6762 §6.7）には、送り手のアドレスと port へ、その ID と問いを付けて TTL 10 で
答え、5353 からの問い合わせには group に答える。`--announce-on ADDR`（繰り返せる）はインターフェースを IPv4 アドレスで選ぶ（既定は
すべてで、OS が group に入れるなら loopback も。Linux は入れる）。`--announce-engine auto|zeroconf|minimal`。1 台の上の CI は、
問い合わせる側と答える側を同じ host で動かす:

```sh
python -m oep_client.virtual_bench_serve --tcp 0 --announce --profile esp32-v003 --unit-id 0123456789ab
# stdout: PORT n（このときには広告が出ている）。stderr: virtual_bench_serve: announcing ...
# それから: _oep._tcp.local. の PTR を一度だけ、一時の port から 224.0.0.251:5353 へ問う（IPv4）- 答えはその port に戻る:
# PTR、SRV（port n）、TXT unit_id=0123456789ab、A 127.0.0.1 - そして tcp://127.0.0.1:n を開く
```

問い合わせの multicast は出ていったインターフェースでこの host に戻り、応答器はすべてのインターフェースで group に入っている。
multicast の経路が無い host では `--announce-on 127.0.0.1` にし、問い合わせを IP_MULTICAST_IF 127.0.0.1 で送る。並んで動く job は
`--unit-id` を変えれば分かれる（instance の名前にも port が入る）。コンテナや VM は自分のネットワークの中でだけ答える。
tests/test_virtual_bench_announce.py がこれを `oep find`、`discovery.find_unit`、`oep://UNIT_ID?via=net`（両方の engine）で試す（同じ process の responder が、OEP でない TCP の
サービスと違う TXT の unit_id を並べて広告し、確かめでそれが外れることも見る）。multicast が
戻らない所では skip する。
