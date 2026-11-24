# `oep` の命令

[English](cli.md)

```sh
oep dump --port <probe>                      # 能力の一覧（--virtual p4-x035 でハードウェアなし）
oep settings show <probe>                    # 全部の値と、保存の状態
oep settings items <probe>                   # 各持ち主が宣言する項目（名前、形、選択肢）
oep settings set <probe> core channel.GPIO23.label=v003.nrst channel.GPIO20.idle=pull-up [--save]
oep settings unset <probe> core channel.GPIO20.idle [--save]     # --all なら全部（その後の --save で保存も空に）
oep settings save <probe>                    # 再起動の後も残す
oep settings state <probe>                   # 保存の状態（ロック不要）
oep settings network <probe>                 # いまのネットワーク: TCP の port、各 netif の様子とアドレス（ロック不要）
oep settings export <probe> jig.oep-settings.json                # 全部の値を名前で、メモ欄のオブジェクトも書き出す（secret はあることだけ）
oep settings import <probe> jig.oep-settings.json [--save]       # 変わる所を見せ、確かめてから set / unset とメモ欄の書き直し
oep speed <probe> [921600,500000] [--verify] [--flows out:2]  # port_speed: UART bridge を速くし（既定 500000）、結果を出す（下）
oep pins <probe> --power 5 --wire swio       # target のつながり方: debug のピン、リセットの線、label（下）
oep clock <probe> [-n 8] [--json]            # fn 0 の clock: uptime_ns と boot_id をこの host の時刻に合わせて（n 回のうち往復が最短のもの）
oep restart <probe> [--reopen-s S]           # oep.probe.restart: probe を再起動し、戻るまで待つ（新しい boot_id）。
                                             # --reopen-s: restart_max_ms のうちに戻らないとき（WSL / usbipd）さらに S 秒まで開き直す
oep find [--timeout 2] [--json] [--no-verify]  # _oep._tcp を広告する probe（確かめたもの）: unit_id、host、port、アドレス
oep linktest <probe> ...                     # --timeout: 既定は応答 1 つに 0.3 秒、TCP では 3 秒
oep call <probe> io.github.you.thing 0x10 a=1 b=0x2   # 任意のインターフェースの op: 番号と hex の要求、または describe の名前と項目で
                                             # （core §7.4 の op_name / op_field）。--text、--wait S（ジョブを status で待つ）、--json
```

`<probe>` は OEP のアドレス `oep://UNIT_ID[?via=経路]`（oep-spec の host ガイド §5.3。via が無ければ USB のデバイス、無ければ
DNS-SD が見つけた probe。どの経路でも describe の unit_id が同じでなければ閉じる）。unit_id を知らないときは、シリアルの口、
`tcp://HOST:PORT`、`tcp://HOST`（その host に DNS-SD が見つけた port）、`usb`（ただ 1 つの probe）でもよい。変更はロックを取り（owner "oep settings"）、終わったら
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

TCP で OEP を運ぶ probe は DNS-SD で見つける（ネットワークの設定は core のもの: transports §7）。

- `oep find` は `_oep._tcp` を browse し（transports §3、host ガイド §4.1）、unit_id、host、port、アドレスを並べる。service の名前
  `oep` は登録した名前ではないので、別のサービスが `_oep._tcp` を広告しうる: どの instance もまず確かめる（`discovery.verify`、
  すべて同時に、答え 1 つあたり 1 秒、`--verify-timeout`）。TCP でつなぎ、confirm（`OEP!` の答え）と fn 0 の describe を送り、
  describe の unit_id が TXT の unit_id と同じものだけを残す。session は開かない。確かめられないものは、その接続先と言ったことを
  stderr に警告して外す。`--no-verify` は広告されたものをすべてそのまま並べる。`discovery.find_unit`（つまり `oep://UNIT_ID?via=net`）も
  同じく確かめ、確かめられない instance は飛ばす。そのあと `open_host` は開いた経路で confirm と describe の unit_id をもう一度
  確かめる。port はいつも SRV
  のもの（決まった port は無い。参照の probe の 7450 は例）。python-zeroconf があれば（`pip install 'oep-client-python[mdns]'`）それで、
  無ければこのパッケージの最小の問い合わせ（QU、一時の port に答えを受け、5353 を共有できればそこでも聞く）で探す。python-zeroconf は
  すべてのインターフェース（InterfaceChoice.All）で、最小の問い合わせも IPv4 のインターフェースそれぞれから送る（インターフェースの
  アドレスごとに IP_MULTICAST_IF。`discovery.interface_addresses`: ifaddr があればそれ、無ければ Linux は各インターフェースのアドレス、
  ほかは host 名のアドレス）。一度の送信は 1 つのインターフェースからしか出ず（Windows ではよく仮想アダプタ）、別のアダプタの probe には届かない。mDNS は同じリンクの
  中だけ: WSL 2 の既定の NAT、VM、ルーターの向こうからは見つからない。そのときは `tcp://HOST:PORT` で名指す。
- `oep://UNIT_ID?via=net` はその unit_id で DNS-SD が見つけた probe を開き、describe の unit_id を確かめる（違えば UnitIdMismatch で閉じる）。
  再起動の後は browse し直す（アドレスは変わりうる）。TCP に認証は無い: 信頼するネットワークかトンネルの中で使う（transports §1）。
- `oep linktest` は TCP では応答を 3 秒待つ（ほかは 0.3 秒）: Wi-Fi の再送でときどき 1〜4 秒止まり、それでも答えは来る。

## ピンを探す（`oep pins`）

`oep pins <probe> [--power CH] [--exclude CH,...] [--wire swio|rvswd|swd] [--pairs D:C,...] [--all-pairs] [--budget S]
[--steps ...] [--name 口] [--save] [--json]` は、ピンを host が選ぶ probe で、target がどこにつながっているかを探します。各段は何をしたかを
出し、全体は `--budget`（既定 55 秒）の中で終わり、最後に plan をすべて解きます。手順はこの client のものです（oep-spec の host ガイド §5.3 が決めるのは label での線の探し方だけ）。target の系統ごとに
要ること（線、リセットのベクタ、リセットの線の有無を読む option の読み方、max_speed、map が読む GPIO の配置と debug の
ピン、パッケージの中で 1 本のリードにつながる GPIO、探す NRST があるか）は 1 つの表 `targets.FAMILIES` にまとめます。

1. **classify**: `oep.fixture.gpio` が許すすべての channel を、pull-up、pull-down の順に 16 回ずつ読む:
   floating（pull-up で 1、pull-down で 0。リセットの線の弱い pull-up のように probe のものより弱い pull もこう読める）、
   driven-high / driven-low（どちらの pull でも同じレベル: push-pull か強い pull。idle-high の UART の線はこう見える）、active
   （読むたびに変わる）。`--power CH` では先に target の電源を切って読み、違って読めた channel が電源に従う。どれも候補を出すだけ。
2. **hold**: floating の channel を 1 本ずつオープンドレインで low に保ち、active の channel を見る。止まればリセットの線の候補。
3. **scan**: floating の channel で線を scan する（rvswd / swd は組で、近い組から。治具は debug の 2 本を隣に並べることが多い）。
   答えが出るか、時間を使い切るまで進む。`--all-pairs` は最後の組まで進み（2 つ目の target が見える）、`--pairs` は試す組を指定する。
   low に駆動された channel も SWDIO として試す（同じ距離の floating の組の後）: 話しかけた後の target は SWDIO を low に保つことがある。
   何も答えなければ線を読み直し、新しく許される組を scan する。全部の組を試していなければ報告がそう言う（`unique-in-partial`、
   `not-finished`）。driven-high / active の channel、電源の channel、`--exclude` は scan しない。
4. **identify**: 答えた線に attach（halt）し、target_id で系統を見る。CH32V00x は option bytes で NRST の有無を見る（読むだけ、
   書かない）。その後 resume。
5. **map**: GPIO の配置が `targets.FAMILIES` にある系統では、target のどのピンが各 channel につながるかを出す: hart を止め、debug の
   ピン以外を浮いた入力にし、各 channel を high、low の順に出して target の INDR を読む（2 回）。classify で driven だった channel は先に
   読み直し（target の出力だったかもしれない）、そのとき floating のものだけを駆動する。電源の channel、リセットの候補、debug の 2 本は
   駆動しない。2 つの channel に追従するピンは、ほかを low に保って試し直す（何も駆動しない線は、隣の線の変化から拾った電荷を保つ）。
   ある端子にだけ追従する channel はその端子のものとし、ほかの端子のものと決まった channel にしか追従しない端子は、配線されていない
   として出さない。ほかの何かが high か low に保つ channel（ボードのプルダウンなど）はプローブから駆動せず、まだ見つかっていない端子を
   target の側から 1 本ずつ出力にして、その channel を読んで探す。
   パッケージの中で 1 本のリードにつながる GPIO はまとめて出す（`PC16/PC11`）。target のレジスタは元に戻し、走らせ直す。
   NRST が無いと option bytes で分かる target 以外は、6 の reset を先に済ませる（知らない NRST を low にすると target がリセットされ、
   debug の接続が切れる）。駆動してはいけないピンを持つ系統（CH32H417 の VIO18 のピンは起動時 1.2 V）は map しない。
6. **reset**: 候補ごとにリセットをかけながら attach する。本物の線なら hart はリセットのベクタで止まる（探す NRST が無い系統では
   飛ばす）。
7. **labels**: host が持つ接続プロファイル（線、ピン、線の設定。probe は配線を持たない）と、label `<口>.swdio` / `<口>.swclk`
   （または `<口>.swio`）、`<口>.nrst` / `<口>.power_hi`（これで target を `oep://<unit_id>/<口>` で開ける）。分かる所はつながる
   target のピンを添える（`<口>.nrst@pa21`。host ガイド §5.3）: debug と NRST のピンは `targets.FAMILIES` から、ほかは map から。
   役目の無いピンは `<口>@pb6`、口のラベルが役目（`rx` など）を持つ線はそれを保つ
   （口は `--name`、無ければ probe の label が名付ける口が 1 つのときその口。どちらも無ければ note で `--name` を求める）を勧める。label は probe の設定に
   対して `settings.find_line` で確かめる（別の channel がもうその名前を持っていれば、線が見つからなくなるので note で言う）。
   `--save` で label を書く（set + save）。`--save` が無ければ何も書かない。

安全: driven-high / active の channel は駆動も scan も保持もしない（driven-low は scan の SWDIO としてだけ）。map は target のピンが
浮いた入力の間だけ channel を駆動する（ほかの何かが保つ channel は読むだけ）。電源の channel は `--power` のときだけ触る。low に保つのは
オープンドレインだけ。gpio の plan を新しくすると、probe は前の plan のピンを（電源の channel も）いったん離すので、`--power`
では新しい plan のたびにきれいに電源を入れ直す（報告の `power_cycles`）。

例（ESP32-P4 と CH32V003、電源は GPIO5）:

```
$ oep pins /dev/ttyACM0 --power 5 --wire swio --name v003
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
label: oep settings set ... core channel.GPIO4.label=v003.nrst   (not written)
label: oep settings set ... core channel.GPIO5.label=v003.power_hi   (not written)
idle:  oep settings set ... core channel.GPIO5.idle=high   (keeps the target powered while no plan holds channel 5; not written by this tool)
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
lease ではその候補を飛ばす）。決めた後は、ほかの速さと同じ試用期間と使用中の判定を受けます。変換によっては、500000 より速い速さが短い確かめを通っても、
長い書き込みで応答を壊します。

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
モードで開く（FTDI では latency timer を 16 から 1 ms にする）。ボードの起動時の速さが 115200 でなければ
`open_host(..., baud=)` で渡す。
