# Pauses inside TCP frames and reconnection

[日本語](tcp-recovery-conformance.ja.md)

Name the SPEC checkout explicitly and run `TcpRecoveryChecks(SampleTcpRecoveryAdapter(), spec=path).run()`.
It matches the body SHAs of core/transports and the wait values. It uses no private equipment settings and no OS socket.
It connects the real LengthFrames/Result decoder with the virtual Core/ResourceModel through a stream with a virtual clock and TCP characteristics.
close/reopen/confirm/replay are explicit steps of a sample Host and do not show reconnection conformance of the production SerialLink.

Resource creation on fn1/op16 is used as a sample extension. max_frame64, boot17, session7/lease60000.

- PAUSE-HEADER/BODY: after a keepalive, a 400 ms pause in the middle of the length or the body. A 100 ms recv returns it as incomplete and keeps the partial bytes. The next recv completes the original result, without reopening the connection.
- LENGTH: receives a length over the limit. The connection is closed and a new parser is created.
- SHAPE: receives a frame whose length fits but is too short for the result header. It does not recover with quiet/confirm on the same connection; it closes and reopens.
- EOF: the stream reports a disconnect. It reopens, then recovers.
- BLOCKED: the confirm response on the reopened transport is also lost. The old request is not resent, and the connection is closed.
- EXPIRED: after the old connection is closed, the resource is released at the lease60000 ms deadline. The same request after reconnecting gets the saved success response, but the state is no lock and no resource. A new request with the old SID gets no_session, and the Host does not treat the old resource number as valid.
- REBOOT: after the old connection is closed, it goes through Endpoint.reboot and re-initialization of the sample extension. In the new boot18, the old request is not resent. It checks the fn0 state and no_session, pages through list to rediscover the fns, then reads the extension state.
- REBOOT-SAME: the case where the fixture repeats boot17. boot_id alone cannot detect the restart, but no lock / no_session drops the old session and resources. The sample redoes list in this case too.

On the reopened transport, confirm is sent first, its corr/header/magic/boot_id are checked, and then all bytes of the original request are resent.
The Probe's session, resources, lease deadline and resend cache are kept apart from the connection's lifetime, and the resend's result and non-re-execution are checked.
The retention cache is compared by a snapshot of internal state; a fixed cache format is not made a wire spec.
The match at 100/400 ms is a condition of a fixture with zero processing time; exact timing is not required of a real CPU or network.

The confirm request is built from OEP? and min/max revision, and all headers including SID and the decoder's exception (ShortPayload) are matched.
The real response bytes of list and the order of state query and rediscovery are also matched. A success in history after expiry is told apart from currently valid resources.
Any post-boot catalog/settings, other race orders with the lease, real OS TCP sockets, the production Host's reconnection steps and notifications/simultaneous connections are not checked.
full_conformance=false.

```sh
uv run pytest -q tests/test_conformance_tcp_recovery.py
```
