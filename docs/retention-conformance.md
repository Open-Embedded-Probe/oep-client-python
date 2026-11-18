# Supplementary checks of the resend cache retention limit and result_lost

[日本語](retention-conformance.ja.md)

core §5.1 keeps records of at least the latest max_inflight requests and the maximum corr, and allows a size limit on keeping request/response bytes. A saved request is compared over all bytes, and a change is malformed. Even for the same request, if the response cannot be kept, result_lost. When the request itself cannot be kept, or the record has been evicted and the corr is at or below the maximum, also result_lost. In none of these cases is anything re-run, the lease renewed or resources changed.

It does not require a specific retention size of general probes. This fixture is `virtual-retention-1` / `io.github.open-embedded-probe.retention` revision 1, with an explicit retention limit of 16 bytes and 8 history entries against max_frame=64. It does not change the normal core/pipeline retention limit of 72 bytes. The transport's declared values are the same as the pipeline sample, 64/80/3, and peer 64/96/2. 8 history entries are at least the max_inflight of both transports.

The own create (op 16) of sample fn 1/2 takes kind u8, reply_size u8, [TLV] and returns resource_id u16, [TLV]. kind is 1/2, and reply_size is the length of the whole response message, 7/16/17. For 16/17, padding with non-critical tag 0x40 is added. Requests are also adjusted to 16/17 bytes with non-critical TLVs. All of them are executable requests within max_frame, and the retention limit is separate from the acceptance limit.


- Request 16 bytes: keeps the whole request and response, and returns them on an identical resend. A changed request with the same corr is malformed.
- Request 17 bytes: keeps a missing-request marker and the small response. An identical or changed request gets result_lost and does not add resources.
- Response 16 bytes: keeps the whole request and response, and returns the identical response.
- Response 17 bytes: keeps the whole request and a missing-response marker. An identical request gets result_lost, a changed request malformed.
- Count limit: after a create, sends 12 keepalives and evicts the first record. At least the latest 3 are kept. The evicted request, and an old unused corr left empty on purpose, get result_lost. With a new corr, the create succeeds exactly once.
- Refusal record: records a 17-byte request with an invalid kind as malformed, and a resend with a missing request gets result_lost. It checks the sample policy that failures and resends do not consume resource numbers.
- end: even when resources are released, the missing markers and history remain, and resends get result_lost. The session is not revived.
- open with a new corr in the same session: changes the initial lease of 3000 ms to 1000/60000 ms, and checks force=0/1 for each. The new open is sent on the peer transport, and resources, existing history, the subscription destination and notification sequence are kept. The real deadline and the response lease are checked independently, and the open itself is also recorded. Old requests resend the saved response or result_lost, and a resend of the same open does not renew the lease. Notifications keep arriving at primary as seq0→1.

- keepalive: 4 conditions renewing lease 1000/60000 ms from both primary and peer. Resources, allocator, subscriptions, notification destination and sequence, and the old cache are kept, and the new request is recorded. The real deadline is renewed by the original lease length from response generation, and an identical resend does not renew.

- owner: 12 conditions of unspecified / 1 byte / 32 bytes of spaces / UTF-8 owner name × end/expiry/force. The original owner is read with lock_state and two kinds of locked refusals, kept by same-S open/keepalive, and cleared on release or on force to a new SID without an owner. All response TLVs are compared byte by byte. Invalid input is checked by the next OWNER-INPUTS. All text values are outside the scope of this case.

- owner input: empty / 33 bytes / invalid UTF-8 / short TLV header / short value / tag0 are malformed; unknown critical is unsupported with the received tag returned. Even on refusal, owner, resources, subscriptions and old history are kept, and the lease renewal, the refusal record and no renewal on resend are checked. Duplicate owners with tags (1,1)/(1,129)/(129,1) use the first. Combinations with an invalid value second are not fixed in this case.

- SID0 identical request: repeats a clock request with the same corr 65535 with 5 ms in between, and returns the current time. It does not change the owned session's cache/high/lease/resources, and detects dedup of old clock responses.

The cache of `state()` includes corr and the actual request/response bytes or None markers, and deadline, high, last, the resource list and next_resource_id are also observed. Before and after a resend, the state itself matches. The 5 ms wait is confirmed with an independent clock so that a wrong deadline renewal is not missed. It also checks that a create with a new corr returns the next number only once. A mere self-report of success counts is not substituted.

`SampleRetentionAdapter` adds `retention_state()` to the explicit lease adapter and returns the retention size and count. It checks the declarations of features/types/geometry conditions/unit/resource ops before session operations. This is model instrumentation and does not add new public ops to hardware.

```sh
uv run pytest -q tests/test_conformance_retention.py
```

```python
import os
from oep_client.conformance import Checks, spec_identity
from oep_client.hardware import equipment_lock
from oep_client.conformance.retention import RetentionChecks
from oep_client.conformance.retention_sample import SampleRetentionAdapter
from oep_client.conformance.retention_model import RetentionModel

registry, spec = spec_identity(os.environ['OEP_CONFORMANCE_SPEC'])
with equipment_lock(os.environ['OEP_HW_LOCK']):
    adapter = SampleRetentionAdapter(RetentionModel())
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), registry, 'virtual-retention-1')
    report = RetentionChecks(checks, adapter).run()
assert report['status'] == 'passed', report
```

Name the SPEC/shared lock of `.env.example` explicitly; the public side does not search for private benches. All raw exchanges, cache/allocator snapshots, wait/control times and SPEC/checker/model hashes are saved to a new artifact.

`full_conformance=false`. Hardware, concurrency, restart, the policy by which the host re-reads the state and recovers from result_lost, combinations of expiry/force with missing retention in recorded real-clock runs, and other retention limits are not checked. The retention of 16 bytes / 8 entries and the policy of not consuming numbers on failure are sample conditions, not additional norms for general probes.
