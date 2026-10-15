# OEP Python client

[English](README.md)

Open Embedded Probe の host 側。v1（oep-spec の本体 `docs/oep-core.ja.md`、`docs/oep-transports.ja.md` とインターフェース `interfaces/*.ja.md`）を話す。凍結の前の v1 で、凍結までは仕様が壊れることがある。番号は oep-spec の
`generated/oep-v1/oep_v1_registry.py` をそのまま写した `oep_client.registry` から取る。破壊的変更を前提とする
実験段階で、互換 API は約束しない。

**実装する仕様: oep-spec の commit `cf69012`**（`v0.x` のタグはまだ無い。oep-spec versioning §2 / §6: 凍結の前は revision 1 だけでは
形が決まらないので、実装は自分が実装する仕様の commit を名乗る）。番号の表と `tests/vectors/` の共有ベクタはその commit から写したもの
（`tests/vectors/SPEC_COMMIT`）。そこまでの変更は [CHANGELOG.md](CHANGELOG.md) にある。

凍結までは日本語の文（`.ja.md`）が仕様の作業の文で、英語の文書は凍結のときにそこから作り、そのときから英語が正になる（それまで oep-spec は英訳を置かない）。OEP を初めて読む人は oep-spec の
[README](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/README.ja.md) と [レビューの手引き](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/review-guide.ja.md)（どこに何が書いてあるか）から。
[使い始める](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/getting-started.ja.md) が最小の probe と host を作り、
[docs/oep-core.ja.md](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/oep-core.ja.md) がプロトコルの本体、[docs/conformance.ja.md](https://github.com/Open-Embedded-Probe/oep-spec/blob/main/docs/conformance.ja.md)
が host の適合に要ることを書く。

target の知識は host にある、という OEP の分担に従う。probe は線と DMI / DP・AP の転送しか知らず、CH32 の flash
コントローラ、RAM ローダー、RP2350 の boot ROM、Cortex-M の debug レジスタなどはここに置く。

```sh
pip install oep-client-python     # PyPI (import oep_client); a checkout: pip install -e <checkout>
uv run pytest                                                            # in a checkout（仮想ベンチ。実機なし）
OEP_HW_BOARDS=<board id> OEP_HW_NOFLASH=1 uv run pytest tests/hw -m hw   # 実機: tests/hw/README.ja.md
```

`import oep_client` だけで使える。番号の表と oep-spec の試験ベクタ（`tests/vectors/*.json`。`tests/test_spec_vectors.py` がこのクライアントと
仮想ベンチを突き合わせる）は、名指した oep-spec の checkout から `OEP_SPEC_DIR=<checkout> tools/sync_registry.sh` で写す（`OEP_SPEC_REF` で
HEAD 以外の commit を選べる。commit とファイルの hash は `tests/vectors/SPEC_COMMIT` / `SPEC_SHA256` に残る）。PyPI の `oep-client` は別のプロジェクトなので、配布名は `oep-client-python`。

リリースは GitHub Actions の Release（workflow_dispatch、version = X.Y.Z か X.Y.ZbN）: `tools/prepare_release.py` が
pyproject.toml、uv.lock、`oep_client.__version__` を書き換え、CHANGELOG.md の Unreleased をその版にし、試験と build の後に commit と tag、
GitHub Release、PyPI（Trusted Publishing）へ出す。変更は CHANGELOG.md の Unreleased に (EN) / (JA) で書き足しておく。

最小構成の確認は [動作確認手順](docs/hardware-quickstart.ja.md)。共通の [テスト方針](docs/testing-policy.ja.md) と [OEP を使うプロジェクトのガイド](docs/testing-consumers.ja.md) も見る。[構成設計](docs/hardware-configuration.ja.md)と[設定形式・雛形](docs/hardware-schema.ja.md)は予定の形式を書く（offline の検査・計画は実装あり、実機の runner との連携は未実装）。

## モジュール（`oep_client`）

下の表のモジュールが公開の API です。直接 import します（`from oep_client import riscv`）。表に無いものと、`_` で始まる名前は
変わることがあります。probe と client の互換性は、実装する SPEC の版、protocol/interface revision、必要な宣言機能と結合試験で確認します。firmware と client の版はそれぞれ記録し、同じ版番号だけで互換性を判断しません。v1 の凍結までは wire が変更されうるため、対応する SPEC と確認した組合せを明記します。

| モジュール | 中身 |
|---|---|
| `host` | 要求と結果（見出しは 10 byte の 1 つ: セッションの中で送る要求はそのセッションの id、セッションの外のロックなしの要求は 0。corr は 0 を使わずに一周し、どの session_id にも共通の 1 つの数）、session_id とロック、`call()`（失敗なら例外）、pipeline、エラーの階層（`OepError` / `Rejected` / `Failed`。セッションが終わった（end、lease の期限切れ、ほかのセッションの force）なら `NoSession`: probe はそのセッションが作ったものをすべて解放している。再開は無く、黙って open し直さず、`Host.session` は None に、`Host.epoch` が進む。probe が知らない connection や stream の番号には `NoResource`。confirm が core §7.1 の範囲の外か、`max_op_ms` が 1〜600000 の外の probe は `NotUsable` で、それ以上何も送らない。`Unavailable` は payload の cause、channel、`fn`（断りが関わる fn）を読む）。`open(lease_ms, force=, owner=)` はいつも新しい乱数の id で新しいセッションを開き、`Opened(lease_ms, boot_id)` を返す。`end()` はすべてを解放し、host はセッションの外に出る。boot_id が変わったとき（confirm、clock、open）は名前 → fn の cache を捨て、list し直す。`clock()` は fn 0 の clock（core §7.6。ロックもセッションも要らない）を `ClockReading(before_ns, after_ns, uptime_ns, boot_id, round_trip_ns)` で返す: 要求を出す直前と応答を受けた直後のこの host の時刻（`time.monotonic_ns`、または `now=`）と、その間に probe が読んだ uptime。`.host_ns` は中点、`.uncertainty_ns` は往復の半分（host ガイド §12）。`clock_best(n=8)` は n 回のうち往復が最短のものを残す。`subscribe(fn, min_bytes, max_delay_ms)` / `unsubscribe(fn)` はそのインターフェース自身の op 0x01 / 0x02 を送る（core §10.3。min_bytes / max_delay_ms はデータだけをまとめ、出来事はすぐ来る。通知を送らない fn は unknown_operation で答える。fn 0 は通知を送らないので、`subscribe(0)` は送る前に ValueError）。`restart_probe(wait_s=None, reopen_s=None)` は名前で探した `oep.probe.restart`（oep-if-restart、host ガイド §5.2。probe が出していなければ LookupError。セッションがロックを持つこと）で probe を再起動する: 応答の後、link は閉じ、少し（約 100 ms、`host.RESTART_AFTER_ANSWER_S`）待ち、新しく開くのと同じに開き直し（最初は confirm。シリアルの口は起動時の速さ、USB の device は列挙し直した後に探し直し、TCP は接続し直す）、`wait_s` の間繰り返す。既定は probe の restart_max_ms（oep.probe.restart の describe。restart の前に読む。probe 自身の再起動だけの上限。`core.restart_max_ms`）に列挙と接続し直しの分の `host.RESTART_EXTERNAL_S`（5 s）を足したもの、宣言が無ければ 10 s。過ぎれば probe は無くなったものとして link を閉じ、ConnectionError / TimeoutError。戻れば新しい boot_id を返す。`reopen_s` は、そうして無くなったものとした probe を利用者が開き直すことを前もって頼むもの: 新しく開くのと同じに、さらにその秒数まで開き直す（probe には分からない遅れで device が戻る host のため - WSL では列挙し直した device を usbipd が付け直す。要ったかどうかは `Host.restart_reopened`）。boot_id が前と同じなら `NotRestarted`。応答が失われ、送り直しが再起動した probe に no_session で断られたときも済んだとみなす。`request_restart()` は要求だけを送る |
| `link` | transport: シリアルの口（常に COBS + CRC、`0x00 <COBS> 0x00`、フレームの外は雑音として捨てる、排他で開く、8N1 で DTR / RTS を立てる）、USB vendor bulk / HID と TCP（長さつきフレーム、transports §5 の立て直し: host の最後の書き込みから probe_frame_gap_ms より長く、250 ms 待つ。TCP ではフレームの途中の休みもそのまま読み続ける）、corr による照合と送り直し（セッションが持つシリアルの口で、答えを待つ間に壊れたフレームが来たらすぐ同じ corr で送り直す - フレームが届き続ける間 `BROKEN_RESENDS` = 3 回まで、host ガイド §8。何も来ない待ちの後は 1 回。上げた port_speed では先にその速さのまま送り直す、下）。送り直しにも応答が無ければ `TransportFailed` を上げ、次の要求の前に confirm で立て直す（入力が静かになるのを待ち、自分の corr の confirm。シリアルの口でも）。立て直せなければ ConnectionError。応答はどれも core §4.4 の下限（その要求の処理上限 + 1000 ms + シリアルの口の転送時間。処理上限は max_op_ms、fn 0 の confirm / list / describe / clock / keepalive / lock_state は 1000 ms まで、max_op_ms を読む前は 1000 ms、引数の待ちがそれより長ければその長さ（`Host.op_bound_ms`）。転送時間は confirm の応答が来るまで min_max_frame で数える、`wait_floor_s`）以上待つ。`Host.open` は先に fn 0 の describe を読む。port_speed の手順の confirm と戻すは link 自身の短い待ちを使う。短い応答は壊れたフレーム。`open_host(target)` |
| `kept_session` | host が次の実行のために probe ごとに残すセッションの id（`KeptSession`、`Host.kept`。open_host が置く。下） |
| `discovery` | ネットワークの probe: mDNS の DNS-SD `_oep._tcp`（transports §3）- `browse(timeout)` -> `Found(instance, unit_id, host, port, addresses)`、`verify(found)` / `check(f)`（confirm と describe の unit_id、host ガイド §4.1）、`find_unit(unit_id)`（確かめたもの）、`port_of(host)`。python-zeroconf があればそれ（`mdns` の extra）、無ければ自前の最小の問い合わせ（下） |
| `core` | インターフェースを名前で探す（キャッシュつき）、confirm（probe の `boot_id` と、この host が来た経路の番号 `transport` つき。2 回目からは使っている revision を求める）、probe の describe（宣言だけ。起動の間 cache: ラベル、transport の一覧、`max_op_ms`）、どの fn の `ops` も（describe の ops tag、core §7.4 - base と 1 byte 以上の bitmap で op 0xFF を越えない - その形を確かめる: 破った fn は使わない（`UnusableFunction`）。fn 0 のものなら probe を使わない（`NotUsable`）。`ops(hst, fn)`、`offers(hst, fn, op)`、`Interface.offers(op)`。`require` / `not_offered` は送らずに、probe が答えるのと同じ `Rejected`（detail unknown_operation）を上げる）、ロックの取り方（`take`）、`oep.probe.plan` でのピンの割り当て（`plan_fn`、`plan_apply`、`plan_release`、`plan_roles`）、`oep.probe.restart`（`restart_fn`、`restart_max_ms`）、`Interface` の土台。`list_entries(hst, name, exact)` は list を first だけで読み進め、名前が合うものを host で残す（core §7.2）。fn 0 の list の項目は捨てる（本体は list に載らない）。`max_op_ms`（宣言の無い probe には `FALLBACK_MAX_OP_MS` 10000）。`oep.probe.link`（oep-if-link）: `link_fn`、`link_speed`、source / sink の要求と応答の部品（`link_size` = source の応答の max_frame − 7、`link_sink_size` = sink の要求の max_frame − 12） |
| `riscv` | `oep.wire.rvswd` / `oep.wire.swio`（scan: 候補の組は host が作る（`candidate_pairs()`）、attach: method・ピンの組・`max_speed` を固定部分で送り、いつも新しい connection（使用中のピンは unavailable）、`reset=(channel, hold_ms)` でリセットをかけながら attach、method 1（halt）は features の attach_halt があるときだけ、detach、connections（connection は u32））、`oep.target.riscv-dm`（応答は値の数を持つ。`reset()` -> `(flags, pc)`（mode 0。flags の bit0 到達）、`reset_halt()` -> dpc。`RunResult.not_halted`、`RunResult.not_run`（準備が失敗し hart を走らせていない）。hart を止め直せなかった step は `step_left` つきの `StepError`。`read_register` / `write_register` は DATA0 を元のまま取っておき（`data_saved`）、`resume` / `step` / `run` は先に書き戻す（`restore_data`、debug §4））、`RiscvDm.declared()`（probe が ops tag で出している任意の op。ほかは unknown_operation）、後始末に失敗した高水準の op（step_left、block の DATA の復元）の後は connection が閉じる、target_id の scheme は `dmi_7f`（`Wire.SCHEME_DMI_7F`）、attach、scan、riscv-dm の reset は引数の時間として max_op_ms 待つ（`attach_ms`、`scan_ms`、`reset_ms`。debug §1、§4.3）、リセット線の探索（`find_reset_line(candidates, pins=...)`）、GPIO 経由の attach |
| `targets` | host が target の系統ごとに知っていることを 1 つの表に（`FAMILIES`: 線、target_id の照合、リセットのベクタ、NRST を option で読む関数、max_speed / idle_clock）。`identify(target_id)` |
| `pins` | `oep pins`: channel の分類、low に保つ探索、scan、識別、リセットの線の確かめ、接続プロファイルと label の提案（`PinFinder`） |
| `console` | `oep.target.console`（位置つきのストリーム: read の応答は長さを持ち、マークは `time_ns` と、共通の kind の lost / clear / host、コンソールの reset 0x40 / restart 0x41 を持つ。ロック不要の `streams()` は stream、connection、mechanism の並び。`open(conn, mechanism)` はいつもその connection に新しいストリーム（u32）を作る - 生きているストリームのある connection への 2 つ目は unavailable cause 6 -。ストリームはセッションのもので、close、connection の解放、セッションの終わりでバッファごと解放され、その番号はその後 no_resource。write はストリームの送りの列に入る - 列の大きさは probe が決め、宣言しない -。accepted = count と列の空きの小さい方で、0 は列が満ちたときだけ。SDI のストリームへの write は unsupported。marks、clear、mark、write は任意）と、バイト列として読み、多くても 1 フレームずつ、応答の accepted の続きから書く `ConsoleIO`（`stall_s`: その間何も受けなければ TimeoutError） |
| `fixture` | `oep.fixture.gpio`（`set([(ch, mode)])` は 1 つの要求で順に当て、`read(channels)` は level を返す。出力の強さは probe 自身のもので、選ぶ手段は無い）/ `uart`（ストリームは plan が作る。`configure(baud, fmt)` までは何も受けない。`status()` = いま効いている `UartStatus(baud, format)` で、configure していなければ baud 0、format 0。probe が立ち上げられなかった configure は `Failed` を上げ、configure していない状態に戻る）/ `i2c-target`（1 つの形: `configure(address)` - target を作り直す -、データのある controller の書き込み 1 回が `read_rx` の 1 フレーム、読み出しには `preload_tx(data)` の置き場か 0xFF で答える、`status()` = `I2cStatus(state, queued, rx_frames, tx_slots, errors)`、ops にあれば `stretch`、`internal_pullups`: 自分の pull-up を入れるか。予約のアドレス 0x00〜0x07 / 0x78〜0x7F は送る前に断る）/ `spi-target`（reset は無く、configure し直す。`cs_setup_ns`: CS の後、最初のビットが確かになるまでの時間。`oep dump` が出す） |
| `config` | `oep.probe.config`（channel ごとの label / idle / disable の項目、count 付きの get / set / unset、保存を持つ probe では save / erase / state。`describe()` = 宣言、`state()` = `State(storage, saved_generation, reason)`。generation は起動から 1 で数える変更の回数: `same_items(a, b)` は項目で比べ、`needs_save()` は保存すると保存が変わるかを言い、`apply(wanted, save=False)` は違う分を set / unset する（host ガイド §15）。`find_line(cfg, "nrst", profile=None)` = host ガイド §5.3 の線の探し方で、firmware の固定 label が最後の段） |
| `capture` | `oep.fixture.logic` / `analog` / `capture-group`（revision 1、oep-spec の oep-if-capture）。configure の TLV は core §2.3 に従う: probe が守れない値は送ったままの tag で unsupported で断られる（表の TLV は critical を付けずに送る）。応答が実際の値で、samples もそれが正しい（アナログ: scale は役割ごとに必ずあり、省かれた skew は 0 と読む）。`start()` はそのたびに新しい世代を返す（`LogicCapture.generation`。同じ起動の間は使い回さず、0xFFFFFFFF の後の start は再起動まで断られる）。read と release はその世代を付け（`read_segment(segment)` は区画自身の世代を付ける）、どの出来事も世代を持つ（`events()` は前の start のものを捨てる）。ストリーミングのデータフレームは世代と自分の区画を持ち、区画をまたがない（segment_info、`Received.segments`）。`status()` は `Status` を返す。capture-group は configure 済みで即時のトリガの、同じ mode のトラックを束ね、一度に始める（`CaptureGroup.start` -> group の start_ns。group と各トラックの世代）。読んだ区画は `Host.on_capture` の callback に `CaptureRecord` で渡る（記録の受け口。wireskein には依存しない） |
| `decode` | キャプチャのチャネルの復号（I2C） |
| `registry` | oep-spec の番号の表から生成したモジュール（編集しない。oep-spec から写し直す）。名前からインターフェースの番号を引く公開の入口は `registry.INTERFACES[name]`（`.revision`、`.op`、`.tlv`、`.enum`。例 `INTERFACES["oep.fixture.uart"].enum["role"]`）。`FIXTURE_UART` などのモジュールの名前は同じもの |
| `arm` | `oep.wire.swd`、`oep.target.arm-adi`、MEM-AP、Cortex-M の停止と関数呼び出し |
| `ch32_flash` | CH32 の書き込み（RAM ローダー、ページ単位の書き直し） |
| `rp2350` | RP2350 の boot ROM 経由の flash と reboot |
| `uiapduino` | UIAPduino のブートローダへの出入り |
| `catalog` / `names` / `interfaces` / `dump` | 能力の一覧と describe の形、表示 |
| `virtual_bench` / `endpoint` / `virtual_bench_serial` / `virtual_bench_serve` / `virtual_bench_mdns` | 仮想ベンチ（下の「仮想ベンチ」） |

## 使い方の例

```python
from oep_client import core, link, riscv, ch32_flash

hst = link.open_host("/run/board-identify/by-id/esp32-series-30eda0e31108")   # pipelining つき
# a serial port (always COBS), "tcp://127.0.0.1:PORT" (a broker), "usb:<unit id>" (the device whose USB serial it is;
# describe's unit_id must match), "usb" (every device on the project's VID:PID 1209:4F45, each counted once: exactly
# one -> vendor, then HID, else its CDC port; several -> link.SeveralProbes lists them, name one) /
# "usb:VID:PID[:SERIAL]" (vendor, then HID). Each is probed first with a
# confirm only (transports §3): no valid answer -> closed, link.NotOepProbe
core.take(hst, 30000, owner="flash script")   # the only way in: force; else wait out the lease, name the holder
wire = riscv.Wire(hst, "oep.wire.rvswd")
conn, _ = wire.attach(halt=True)
dm = riscv.RiscvDm(hst, conn)
dm.reset_halt()
result = ch32_flash.program(hst, dm, open("sketch.bin", "rb").read(), ch32_flash.PROFILES["x035"])
dm.reset()                                    # ndmreset して走らせる -> (flags, pc)
wire.detach(conn)
hst.end()
```

## Linux での USB の権限（udev）

probe はプロジェクトの USB の VID:PID `1209:4F45`（transports §3）で列挙される。その vendor bulk（libusb）や HID（hidraw）を
普通の利用者で開くには udev の規則が要る: [`udev/70-oep-probe.rules`](https://github.com/Open-Embedded-Probe/oep-client-python/blob/main/udev/70-oep-probe.rules)（checkout と sdist にある）は、ログインしている
利用者（`uaccess`）と `plugdev` グループに USB device とその hidraw を開かせる。入れるには管理者の権限が要る:

```sh
sudo install -m 0644 udev/70-oep-probe.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger   # または probe を抜き差しする
```

probe の CDC の口（`/dev/ttyACM*`）は、いつもの `dialout` グループのほかに規則は要らない。Windows と macOS では規則は要らない。

## `oep` の命令

```sh
oep dump --port <probe>                      # 能力の一覧（--virtual p4-x035 でハードウェアなし）
oep config show <probe>                      # 設定、宣言、保存の状態
oep config state <probe>                     # 保存の状態だけ（ロック不要）
oep config label <probe> 23 nrst             # ほかに idle CH MODE、disable CH...、remove KIND CH（どれも --save を取る）
oep config save <probe>                      # 再起動の後も残す（erase もある）
oep speed <probe> [921600,500000] [--verify] [--flows out:2]  # port_speed: UART bridge を速くし（既定 500000）、結果を出す（下）
oep pins <probe> --power 5 --wire swio       # target のつながり方: debug のピン、リセットの線、label（下）
oep clock <probe> [-n 8] [--json]            # fn 0 の clock: uptime_ns と boot_id をこの host の時刻に合わせて（n 回のうち往復が最短のもの）
oep restart <probe> [--reopen-s S]           # oep.probe.restart: probe を再起動し、戻るまで待つ（新しい boot_id）。
                                             # --reopen-s: restart_max_ms のうちに戻らないとき（WSL / usbipd）さらに S 秒まで開き直す
oep find [--timeout 2] [--json] [--no-verify]  # _oep._tcp を広告する probe（確かめたもの）: unit_id、host、port、アドレス
oep linktest <probe> ...                     # --timeout: 既定は応答 1 つに 0.3 秒、TCP では 3 秒
```

`<probe>` はシリアルの口、`tcp://HOST:PORT`、`tcp://HOST`（その host に DNS-SD が見つけた port）、`tcp:UNIT_ID`（TXT の unit_id で
DNS-SD が見つけた probe。describe の unit_id が同じでなければ閉じる）、`usb[:VID:PID[:SERIAL]]`。変更はロックを取り（owner "oep config"）、終わったら
セッションを閉じる。変更はすぐ効き、`save` の後は再起動しても残る。

### 走り直す host: 残したセッションの id

probe は経路が閉じてもセッションを保つ（transports §3）ので、殺された、落ちた、線を失った命令は、lease が切れるまでロックと
持っていたもの（connection、plan、購読）を残す。そこで `link.open_host` は、host が開いたセッションの id を probe ごとのファイルに
残し（`kept_session.KeptSession` を `Host.kept` に。host ガイド §5）、host の最初の open の前に、前の実行が残したセッションを終える:
その id で open し（ロックを持つセッションの open はそのセッションを保つ。core §6.2: 何も解放しない）、すぐ end する。それから自分のセッションを開く。
この 2 つの要求は、前の実行が送ったどの corr よりも先の corr で送る（core §5.1）: ファイルは id と一緒に、その実行が使った先に取って
おいた corr（1024 ずつ取り直す）を残し、次の実行はその先から続ける。それの無いファイルでは何も終えない。
`oep` の命令と tests/hw は `open_host` で開くので、こうなる。ほかの長く動く host は `keep_session=True`（既定）を渡すか、自分で作った
Host に `hst.kept = kept_session.KeptSession()` を置く。

- ファイルは `<dir>/<unit_id>.session`（id と取っておいた corr を 16 進で `ssssssss cccc`。`end` の後は空）。fn 0 の unit_id をキーにするので、同じ probe の別の口や
  経路でも同じファイル。`<dir>` は `$OEP_SESSION_DIR`、無ければ `$XDG_RUNTIME_DIR/oep-client`、無ければ
  `%LOCALAPPDATA%\oep-client\sessions`（Windows）か `${XDG_CACHE_HOME:-~/.cache}/oep-client/sessions`。
- host はファイルを持つ間、そのファイルに排他の錠（flock / msvcrt.locking）を掛け、link を閉じるかプロセスが終われば外れる。
  ほかの動いている host が持つファイルには触れない: その host のセッションはその host のもので、この host はふつうに開く（その
  ロックに会い、`core.take` が待つか持ち主を示す）。`x-` の unit_id は個体を名指さないので、何も残さない。
- `Host.end_previous(sid)` がこの手順そのもの: そのセッションを終えたら True、別のセッションがロックを持てば False（この host の
  終えるものではない）。`keep_session=False` は何も読み書きしない。

### TCP

TCP で OEP を運ぶ probe は DNS-SD で見つける（ネットワークの設定は oep.probe.config のものではない）。

- `oep find` は `_oep._tcp` を browse し（transports §3、host ガイド §4.1）、unit_id、host、port、アドレスを並べる。service の名前
  `oep` は登録した名前ではないので、別のサービスが `_oep._tcp` を広告しうる: どの instance もまず確かめる（`discovery.verify`、
  すべて同時に、答え 1 つあたり 1 秒、`--verify-timeout`）。TCP でつなぎ、confirm（`OEP!` の答え）と fn 0 の describe を送り、
  describe の unit_id が TXT の unit_id と同じものだけを残す。session は開かない。確かめられないものは、その接続先と言ったことを
  stderr に警告して外す。`--no-verify` は広告されたものをすべてそのまま並べる。`discovery.find_unit`（つまり `tcp:UNIT_ID`）も
  同じく確かめ、確かめられない instance は飛ばす。そのあと `open_host` は開いた経路で confirm と describe の unit_id をもう一度
  確かめる。port はいつも SRV
  のもの（決まった port は無い。参照の probe の 7450 は例）。python-zeroconf があれば（`pip install 'oep-client-python[mdns]'`）それで、
  無ければこのパッケージの最小の問い合わせ（QU、一時の port に答えを受け、5353 を共有できればそこでも聞く）で探す。python-zeroconf は
  すべてのインターフェース（InterfaceChoice.All）で、最小の問い合わせも IPv4 のインターフェースそれぞれから送る（インターフェースの
  アドレスごとに IP_MULTICAST_IF。`discovery.interface_addresses`: ifaddr があればそれ、無ければ Linux は各インターフェースのアドレス、
  ほかは host 名のアドレス）。一度の送信は 1 つのインターフェースからしか出ず（Windows ではよく仮想アダプタ）、別のアダプタの probe には届かない。mDNS は同じリンクの
  中だけ: WSL 2 の既定の NAT、VM、ルーターの向こうからは見つからない。そのときは `tcp://HOST:PORT` で名指す。
- `tcp:UNIT_ID` はその unit_id で DNS-SD が見つけた probe を開き、describe の unit_id を確かめる（違えば UnitIdMismatch で閉じる）。
  再起動の後は browse し直す（アドレスは変わりうる）。TCP に認証は無い: 信頼するネットワークかトンネルの中で使う（transports §1）。
- `oep linktest` は TCP では応答を 3 秒待つ（ほかは 0.3 秒）: Wi-Fi の再送でときどき 1〜4 秒止まり、それでも答えは来る。

実機での一通りの確認は ArduinoCore-CH32 の `tests/manual/oep_smoke/`（`oep_smoke.py`、`oep_probe_checks.py`）。

## ピンを探す（`oep pins`）

`oep pins <probe> [--power CH] [--exclude CH,...] [--wire swio|rvswd|swd] [--steps ...] [--save] [--json]` は、ピンを host
が選ぶ probe で、target がどこにつながっているかを探します。各段は何をしたかを出し、全体は 1 分以内に終わり、最後に plan を
すべて解きます。手順はこの client のものです（oep-spec の host ガイド §5.3 が決めるのは label での線の探し方だけ）。target の系統ごとに
要ること（線、リセットのベクタ、リセットの線の有無を読む option の読み方、max_speed / idle_clock）は 1 つの表 `targets.FAMILIES`
にまとめます。

1. **classify**: `oep.fixture.gpio` が許すすべての channel を、pull-up、pull-down の順に 16 回ずつ読む:
   floating（pull-up で 1、pull-down で 0。リセットの線の弱い pull-up のように probe のものより弱い pull もこう読める）、
   driven-high / driven-low（どちらの pull でも同じレベル: push-pull か強い pull。idle-high の UART の線はこう見える）、active
   （読むたびに変わる）。`--power CH` では先に target の電源を切って読み、違って読めた channel が電源に従う。どれも候補を出すだけ。
2. **hold**: floating の channel を 1 本ずつオープンドレインで low に保ち、active の channel を見る。止まればリセットの線の候補。
3. **scan**: floating の channel で線を scan する（rvswd / swd は組で、600 組まで）。driven / active の channel、電源の channel、
   `--exclude` は scan しない。
4. **identify**: 答えた線に attach（halt）し、target_id で系統を見る。CH32V00x は option bytes で NRST の有無を見る（読むだけ、
   書かない）。その後 resume。
5. **reset**: 候補ごとにリセットをかけながら attach する。本物の線なら hart はリセットのベクタで止まる。
6. **labels**: host が持つ接続プロファイル（線、ピン、線の設定。probe は配線を持たない）と、label `nrst` / `power_hi`
   （`--name` を付けると `<名前>.nrst` / `<名前>.power_hi`。複数の target を受ける probe 向け）を勧める。label は probe の設定に
   対して `config.find_line` で確かめる（別の channel がもうその名前を持っていれば、線が見つからなくなるので note で言う）。
   `--save` で label を書く（set + save）。`--save` が無ければ何も書かない。

安全: driven / active の channel は駆動も scan も保持もしない。電源の channel は `--power` のときだけ触る。low に保つのは
オープンドレインだけ。gpio の plan を新しくすると、probe は前の plan のピンを（電源の channel も）いったん離すので、`--power`
では新しい plan のたびにきれいに電源を入れ直す（報告の `power_cycles`）。

ESP32-P4 と CH32V003（電源は GPIO5）、2026-10-02。2026-10-07 の変更より前の、両方の pull でも読んでいた頃の実行で、その
"pulled-up" の行（channel 4、リセットの線の弱い pull-up）はここでは除いた - pull-up と pull-down だけなら floating と読める:

```
$ oep pins /run/board-identify/by-id/esp32-series-30eda0ea068b --power 5 --wire swio
power: channel 5 low 300 ms (target off: read), then high 400 ms before the reads
classify: 52 channels, 16 reads each under pull-up, pull-down
  floating     0-3,6,9-20,26-34,36-50,52-54
  driven-high  7-8,22-23,35  (never scanned or held)
  driven-low   51  (never scanned or held)
  active       21  (21: 8 changes under pull-up)
  follow power 4,6,9-11,13,15-16,19-23,32-33  (read otherwise with the target off: wired to it; candidates only)
reset line (hold low): 45 candidates, each held low (open drain) up to 390 ms while watching 21 (105 changes in 1.0 s running, longest lull 130 ms)
  hold 4 low: 21 stopped
  45 held in 3.4 s: stopped by 4
scan swio: 45 channels (0-4,6,9-20,26-34,36-50,52-54; not 7-8,21-23,35,51: driven / active) in 0.14 s -> 19
attach swio 19: target_id 00310510 (WCH DMI 0x7F) -> ch32v00x, halted at dpc 0x108
  option bytes (read only): RST_MODE 10 (USER 0xf7): NRST on PD7, 12 ms ignore window
  attach under reset through 4 (held 20 ms): dpc 0x0 -> the reset line
profile: wire swio, pins 19   (the host's to keep: the probe keeps no wiring)
label: oep config label ... 4 nrst   (not written)
label: oep config label ... 5 power_hi   (not written)
idle:  oep config idle ... 5 output-high   (keeps the target powered while no plan holds channel 5; not written by this tool)
released every plan (channel 5 is back to its idle state: the target is powered only while something drives it)
done in 7.3 s
```

19 が SWIO、4 が NRST（hold で 21 が止まり、リセットをかけながらの attach で dpc 0）。22 / 23 は target の UART（idle high
なので driven として scan も保持もしない）、21 はアプリが動かす出力。

## UART bridge を速くする（port_speed、使うときだけ）

任意の `oep.probe.link` が ops に `port_speed` を立てる probe（oep-if-link §3。参照の classic ESP32 の firmware は立てる）では、host は 1 つのセッションの
間、UART bridge を起動時の速さ（115200）より速くできます。host が頼まない限り何も変わりません:

```python
hst = link.open_host("/dev/ttyUSB0", port_speed=True)    # 500000。ロックを取り、セッションを開いたままにする
print(hst.link.speed.to_text())             # 候補ごとに、基準、流し方、今の速さ
# 完全な形（基準と流し方を測る）を取ってあるセッションの中で:
report = link.raise_speed(hst, [500000], verify=True, flows=[("out", 2)], record=True)
# 利用者が選んだときだけ速い速さ（例: `oep speed <probe> 921600,500000`）: 先に 1 秒の確かめ
hst = link.open_host("/dev/ttyUSB0", port_speed=[921600, 500000])
```

**既定の上限は 500000**（この client の選び方。oep-if-link §3 は、どの速さを試すか、どう確かめるか、いつ戻すかを host に任せる）: 既定の候補は 500000 だけ（`link.DEFAULT_CANDIDATES`）で、利用者が明示して
選ばない限り、それより速い速さは試しません。`link.DEFAULT_CEILING` より速い速さを候補に入れるのは、利用者がそれを選んだとき
（`oep speed <probe> 921600,500000`、設定）だけにしてください。その速さは、最小の形でも完全な形でも、**1 秒の確かめ**を通ってから
決めます: 試しの状態で、oep.probe.link の source（in、max_frame − 7）と sink（out、max_frame − 12）の
いっぱいのフレームを、完全な形が duplex を確かめるなら duplex でも、それぞれ 1 秒以上（`link.FAST_VERIFY_S`）その同時数で流し、
流し方と同じ基準で判定します（n = 1 での流し直しはしない）。その試すは verify_ms 4000（duplex を含めば 6000）を頼むので、lease は
5000（7000）ms 以上が要ります（`link.lease_for(candidates, flows, verify)`。`open_host` と `oep speed` は少なくともそれで取り、短い
lease ではその候補を飛ばす）。決めた後は、ほかの速さと同じ試用期間と使用中の判定を受けます。理由: ある変換では 921600 が両方向
16 フレームずつの確かめを通っても、9 KiB の書き込みのたびに応答が 1〜4 個壊れた。500000 は測ったどの変換でも壊れなかった。

手順はこの client のもの。oep-if-link §3 は握手だけで、port_speed の要求は baud、step（試す / 決める / 戻す）、verify_ms、効くのは要求が来た
UART bridge の口で、起動時より速い速さだけ。port_state（ロック不要）はその口の baud と phase（0 起動時、1 試し、2 決めた、3 フレームの
切れ目を待つ戻り）を読む。**最小の形**（既定、約 50 ms、計測なし）: 候補ごとに順に
`試す`（今の速さで応答してから probe が切り替える）→ host は要求した baud に切り替える → 20 ms → `confirm`（100 ms、3 回まで）→
`決める`。**完全な形**（`verify=True` か `flows=` を渡す）: 起動時の速さの基準を流し方ごとに取り（このセッションのフレーム、
無ければ 60 フレーム）、候補ごとに使う流し方だけ流す。流し方 = `("in"|"out"|"duplex", n)`（in = oep.probe.link の source probe → host、
out = oep.probe.link の sink host → probe、duplex = 両方を交互。`n` は同時数、0 = link が出す最大）で、いっぱいのフレーム（source は
max_frame − 7、sink は max_frame − 12）を 16 個流し、
壊れと失われを数え KB/s を測る。壊れ + 失われが 3 以上で割合が max(基準 × 2, 5 %) を超えたら流し方は通らず、n = 1 で流し直し
（通れば n = 1 が link の上限）、1 つでも通らなければ候補は通らない。通らない候補は戻して（step 2）起動時の速さに戻り confirm し直し、新しい port_state で phase 0 を確かめる（`link.port_state`）。
最初に通った候補を使う。probe の UART が作れない速さ（port_speed_tolerance_pct、2 % の内で）は断られ、飛ばす。通る速さは変換チップとドライバで決まる（クロックの整数分周しか
作れないもの、短い確かめは通るのに 500000 より速い速さで長い burst を壊すものがある）ので、速さは host が上限の内で選ぶ。
結果（`link.speed`: `base`、`rate`、`chosen`、`baseline`、`trials` = `flows` と `in_kb_s` / `out_kb_s` / `duplex_kb_s` を持つ
`SpeedTrial`）はキャプチャや書き込みの予算を立てるのに使う。

probe が自分で起動時の速さに戻るのは、試すが verify_ms を過ぎたとき、決めた後に正常なフレームの無いまま port_speed_idle_ms
（3 秒、`link.IDLE_MS`。落ちた host の速さもそれより長くは残らない）が過ぎたとき、セッションが終わったとき（`end`、lease の期限切れ、
force）だけ - 壊れたフレームだけでは戻らない。上げている間、link は 1 秒（`link.KEEPALIVE_S`）黙れば要求の前に keepalive を送り、
長く黙る呼び出し側は `hst.link.keep_alive()` で同じことをする。シリアルの口を開くと最初の confirm を port_speed_idle_ms +
host_wait_add_ms（4 秒、`link.OPEN_RETRY_S`）繰り返して、残った速さが戻るのを待つ。link は `end`、戻す、restart、別のセッションの force の open にはすぐ合わせる。
上げた速さで応答の来ない要求は、まずその速さのまま送り直す: 最初の待ちは多くても port_speed_idle_ms
の 3 分の 1（`link.RAISED_FIRST_WAIT_S`）と lease の 4 分の 1 で、core §4.4 の下限は下回らないので、送り直しはまだその速さの probe に
届く。その送り直しにも応答が無いときだけ、起動時の速さに戻って confirm し、下げて、そこでもう一度送る（固まらない）。使っている間、決めた速さの最初の 32 KiB と 1 秒（`probation_bytes`、
`probation_s`）は試用期間で、そこで壊れ・失われが 3 以上かつ max(基準 × 2, 5 %) 超、または応答が来なければ、確かめの失敗として
すぐ下げる（16 フレームの確かめは素早い関門として残す）。その後は直近 3 秒のフレーム（50 未満なら判定しない）を見て、
max(基準 × 2, 10 %) を超えて壊れ・失われたら下げる。下げるのは、上げた速さで port_speed の戻すを送り、起動時の速さに戻って confirm
し、その呼び出しの候補のうちこのセッションで通らなかったものより下の次の候補を新しく試す → confirm → 確かめ → 決める（残って
いなければ起動時の速さ）。壊れた速さとそれより上はそのセッションの間は使わない（`speed.stepped_down`、`speed.down_why`、
`to` と `probation` を持つ `speed.step_downs`）。`max_tries=` は 1 回の呼び出しで試す候補の数の上限（キャプチャの host は 2）。
`record=True`（真偽値・パス・`speed_record.SpeedRecord`。`oep speed` CLI は既定で ON、ライブラリは OFF）は、通った / 通らなかった
速さを（口、unit_id）ごとに `~/.cache/oep-client/link-speed.json` に残し（通ったは 30 日、通らなかったは 1 日、別の速さの破綻から
2 秒（`settle_s`）以内に測った失敗は「不明」）、通った速さを先頭に、通らなかった速さを外す。全部の候補が通らなかったとあれば、
いちばん遅い候補を 1 回だけ試す（`speed.retried`）。`x-` で始まる unit_id（一意の番号も保存先も無い probe、core §7.5）では
何も残さず何も読まない。上げるのは、この host が来た経路（confirm の応答の transport TLV、core §7.1）が UART bridge のときの
その口。UART bridge でない口に来た port_speed の要求と、口を上げている間の試すは unavailable（cause 6）で断られる。`oep.probe.link` の無い probe、または `oep.probe.link` が port_speed を持たない probe では
速さは変わらない（結果がその理由を言う）。線の試験（`linktest`、`core.link_speed`）も `oep.probe.link` を要る。ブローカー（TCP）の後ろでは client ではなくブローカーが行う。シリアルの口は、ドライバにあれば low-latency の
モードで開く（FTDI の latency timer 16 → 1 ms で UART bridge の速度が 3 倍になった）。ボードの起動時の速さが 115200 でなければ
`open_host(..., baud=)` で渡す。

## 仮想ベンチ（動く spec）

仮想ベンチは、probe とその先の target、治具の配線を、実際の治具に合わせてソフトウェアで作ったもので、ハードウェアなしで host の
ソフトウェアを試す環境である（この repo で「ベンチ」とだけ書けば実機の HIL の治具を指し、ソフトウェアのほうは常に「仮想ベンチ」と書く）。

その probe、`endpoint.Endpoint` は oep-spec cf69012 の規範どおりに答え（応答のデータと並びは長さを持ち、どの応答にも TLV が
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
fn。書き込み位置以降の read は write_pos と len 0 を答える。probe.config: label / idle / disable の項目（idle は 3 byte。長さが形と違う
項目は malformed）。generation は起動から 1 で数える変更の回数。state は storage_state、saved_generation、reason。起動時は保存した項目を
1 つずつ確かめ、disable を先に当て、当てられないものは外す（部分適用）。port_speed: 要求が来た UART bridge の口への
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
`--framing cobs` は指定したときだけで、socket の上でシリアルの口をまねる（COBS のフレームだけ、一度に 1 つの接続）。length の framing の TCP は同時に `--tcp-connections N` 個（既定 3。参照の probe と同じ。それを超える接続は受けてすぐ閉じる）の接続に答える。どの接続も別の経路（transports §1: confirm と使っている revision は接続ごと、通知はその fn の subscribe が来た接続へ）で、probe は 1 つ: ある接続のセッションがロックを持つ間、別の接続の open は locked で断られ、lock_state はどの接続からも同じに見える。`--framing cobs`（シリアルの口）は今も一度に 1 つの接続に答える。閉じた接続はセッションを終えない（transports §3）: セッション、ロック、購読、送り直しの表は lease が切れるまで残り、閉じた接続への通知は捨て、別の接続からの同じ id の open がセッションを取り戻し、通知もその接続に移る（core §6.2）。`--once` は、接続を 1 つ受けた後に接続が 1 つも無くなったら終わる（length の framing では、重なった接続の最後が閉じたとき）。故障の注入は `--drop N`（N 番目の答えを 1 回出さない。要求は実行済みなので送り直しは覚えた答えを
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
tests/test_virtual_bench_announce.py がこれを `oep find`、`discovery.find_unit`、`tcp:UNIT_ID`（両方の engine）で試す（同じ process の responder が、OEP でない TCP の
サービスと違う TXT の unit_id を並べて広告し、確かめでそれが外れることも見る）。multicast が
戻らない所では skip する。

v0 の client（`oep_client.v0`）は 2026-09-26 に消した（git の履歴に残る）。v0 を話す probe はもう無い。
