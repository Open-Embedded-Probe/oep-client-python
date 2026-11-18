# Wrap and release of capture segment serials

[日本語](capture-serial-conformance.ja.md)

Name the SPEC checkout explicitly and run `CaptureSerialChecks(SampleCaptureSerialAdapter(), spec=path).run()`.
Besides core/transports, it matches the reviewed SHAs of the capture/common body text. It uses no private equipment and no real transport.
It sends real wire requests and samples to the current independent logic/analog models and tells apart the u32 serial and the u64 byte position.

4 conditions: logic/analog combined with start serial 0/0xFFFFFFFE.
repeat, 2 samples/segment, capacity of 2 segments, 1000Hz, max_frame256, session7. The position starts at 2^40+17.
After start, it explicitly places a seed of "a prefix not retained", which stands for many past captures already released.
This is a finite logical boundary check, not evidence that billions of real captures were run.

It captures two segments and pauses, then checks the next serial and each segment's metadata and data.
It matches with real bytes the inclusive from of segments, an empty response from next, and the oldest fallback from an old/future number.
release is inclusive. An old number releases nothing; releasing the oldest resumes, and the next segment has a gap.
A resent release does not release a new segment. A release with a wrong generation leaves the capture unchanged with cause6.
A release of a future serial also does not drop the unfinished current sample; stop completes it as a short segment.
The read request is generation(u32)·position(u64)·max(u16), and the response is position(u64)·flags(u8)·len(u16)·data (capture §3.2).
read returns a u64 position: gap for a position already released, and empty from write_pos for a future position not yet captured.
For an old position after all finished segments are released, position=write_pos, gap=1, len=0. max=0 is also an empty success with the same position/gap rules.
A read with a wrong generation is also refused with cause6 and leaves the capture unchanged. The first u32 of the start response is used as the generation.
The segments' position, samples, time, flags, generation and the raw logic/analog bytes are built independently and matched.

At most 2 segments are kept, and the difference to the next serial is less than 1/4 of u32. This is not taken as proof for any retention capacity or the whole retention range.
To move this common segment procedure to another capture extension, prepare an explicit adapter and an independent sample/packing oracle.
Mark retention, interleaving with streaming/group notifications, arbitrary capacity, physical sampling and integration with the production Host are not checked.
full_conformance=false.

```sh
uv run pytest -q tests/test_conformance_capture_serial.py
```
