# Retention range of notification seq

[日本語](notification-retention-conformance.ja.md)

Name the SPEC checkout explicitly and run `NotificationRetentionChecks(SampleNotificationRetentionAdapter(), spec=path).run()`.
It matches the body SHAs of core/transports and the expected registry values. It uses no private equipment and no real transport.
It checks core §2.6 "the difference between wrapping values held at the same time is less than 2^(w−2)" with an explicit sample of u16 notification seq.
The sample's next-seq counter is also included in the same retention space, and the difference between the counter and the remaining frames is kept below 16384.
This is a conservative retention policy of the sample, not a rule that puts all past notifications not retained into the retention range.
The pinned body text is shared with the [response wait deadline checks](host-deadline-conformance.md) (core §2.6/10.3/10.4, transports §3).
core §10.4 states "no other response or notification is inserted into a frame that has started sending", "a notification that does not fit is dropped before it starts sending"
and "what remains to be sent on a closed transport is dropped". This check confirms that as no replacement of active, dropping unsent, and dropping on close.

4 conditions: start seq 0/65530 combined with unsent pending / active with only the first byte sent.
session7/lease60000, virtual clock 0, max_frame64, 2 fns / 1 transport. fn1 really generates 16386 notifications, and fn2 keeps 1 notification.
Events and data are generated alternately, and it checks that the shared seq advances even when dropped.
At the points where 1, 16383, 16384 and 16386 have been generated, it observes the counter, all bytes of retained frames, the active offset, the byte amount and the transport state.

Below the boundary, the oldest frame is kept. Where the difference reaches 16384, unsent frames are dropped, and fn2's separate space is kept.
An active frame is not replaced by another frame midway; the transport is closed and it is dropped. Even when closed, the Core session/subscription remains.
Notifications generated again are not queued on the closed transport, and seq advances. This close/eviction is the realization the sample adopts.
What the norm specifies is the retention range and non-interleaving; it does not fix the concrete buffer management of another conforming Probe.
`out` is treated as the Host's receive mailbox for what was sent; the Probe's unsent retention area is push and active.

RouteModel limits retention by the deadline of unsent frames as well as the byte amount, and when active reaches the boundary it logically closes the transport.
The unit tests have normal, observation-fault, real-model-fault and missing-control cases.
Other wrap spaces such as capture/dmseq, the Host's retained values, concurrent producers and OS/real writer buffers are checked separately.
full_conformance=false.

```sh
uv run pytest -q tests/test_conformance_notification_retention.py
```
