# 通知seqの一周・再購読・再送の検査

`NotificationSequenceChecks`はcore §5.2/11.2/11.3/11.4、transports §3に基づく第二段階の補助検査。
公開`RouteModel`と`SampleNotificationSequenceAdapter`を使い、選択SPEC checkoutを明示する。
privateベンチや隣接checkoutの探索は行わない。

前提1、共通describe 2、動作7の計10項目を持つ。

| 動作 | 観測 |
|---|---|
| WRAP | seq 65534からdata/eventを交互に出し、65535→0→1。同じfnで共有し、別fnは0から独立 |
| DROP-WRAP | 送信停止中の32フレームでmax_frame×2の制限とdropを確認。一周前後を含め、捨てたフレームも番号を消費 |
| REPLACE | seq/送り先の置換。別fnのseq/送り先を変えず、一つの通知を両経路へ出さない |
| UNSUBSCRIBE | 未購読・解除・再解除が成功し、別fnの購読/seq/条件/経路と既存資源を保持。解除後の新通知は送らず、再購読でseq0 |
| REPLAY | 古い成功subscribeの再送は同じ応答を返す。新しい購読のseq/送り先/条件を巻き戻さない |
| REPLAY-REFUSED | 古い拒否subscribeの再送も保存応答を返し、新しい購読に触れない |
| REJECTED | malformed/未知critical TLVの拒否前に購読seq/送り先/条件/資源を変更しない |

fixtureはfn 1/2、primary/peer、max_frame 64を明示する。seqのseedと論理resetは検査用のinstrumentationで、
実機の公開opではない。seedは購読済みfn・通知queueが空のときだけ可能。
保持する通知の特定の選び方を規定せず、残ったフレームの内容と一周を含む生成順序、次のseqを照合する。

送り先置換の検査は、順序を固定したraw要求を同じSIDでprimary/peerへ送る。
これはprobeの「経路を問わず同じ要求として処理する」「再送では送り先を変えない」を観測するための刺激である。
transports §3はHostに一つのセッションを一経路で使うことを要求するため、通常のHost利用例にこの刺激を流用しない。
競合する経路遅延の誤判定を適合と扱わず、Hostの一経路制約自体はこの検査で検証しない。

client checkoutのルートで実行する。

```sh
uv run pytest -q tests/test_conformance_notification_sequence.py tests/test_conformance_routes.py tests/test_conformance_data.py tests/test_conformance_subscriptions.py
```

新しいpytestは正常モデル、10種類の違反注入、無効な前提3種類の14件。
既存のroute/data/subscriptionと合わせて92件。応答優先・部分送信・queue量・経路切断は既存RouteChecksを別に実行する。
SPEC identity、送受信raw、刺激/観測、checker/model SHAを保存する例:

```sh
uv run python - /path/to/oep-spec /path/to/notification-report.json <<'PY'
import hashlib, json, sys
from pathlib import Path
from oep_client import virtual_route_model
from oep_client.conformance import Checks, checker_sources_sha256, spec_identity
from oep_client.conformance_routes import RouteChecks
from oep_client.conformance_route_sample import SampleRouteAdapter
from oep_client.conformance_notification_sequence import NotificationSequenceChecks
from oep_client.conformance_notification_sequence_sample import SampleNotificationSequenceAdapter
from oep_client.virtual_route_model import RouteModel
registry, identity = spec_identity(sys.argv[1])
model = RouteModel()
routes = RouteChecks(Checks(model.handle, registry, 'virtual-routes-1'), SampleRouteAdapter(model)).run()
model = RouteModel()
checks = Checks(model.handle, registry, 'virtual-routes-1')
report = NotificationSequenceChecks(checks, SampleNotificationSequenceAdapter(model)).run()
report['sequence_status'] = report['status']
report['status'] = 'passed' if report['sequence_status'] == routes['status'] == 'passed' else 'failed'
report.update(spec=identity, route_checks=routes, wire_trace=checks.trace,
              checker_sources_sha256=checker_sources_sha256(),
              model_sources_sha256={name:hashlib.sha256((Path(virtual_route_model.__file__).parent/name).read_bytes()).hexdigest()
                  for name in ('virtual_route_model.py','virtual_data_model.py','virtual_notification_model.py',
                               'virtual_resource_model.py','virtual_core.py','endpoint.py','virtual_bench.py')})
with Path(sys.argv[2]).open('x') as output:
    json.dump(report, output, indent=2)
    output.write('\n')
raise SystemExit(report['status'] != 'passed')
PY
```

全OEP固有通知のpayload、実際のOS/framing/writer buffer、並行producer、Host動作、
全経路容量とcore/共通全規範の適合は未検査として残す。
