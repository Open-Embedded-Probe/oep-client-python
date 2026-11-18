# Raw USB conformance checks

[日本語](usb-conformance.ja.md)

Probe input over bulk/HID is checked with independent checkers of the public client. It does not go through the normal `usb_stream` / `hid_stream` encoder/decoder, resend or reframing. The basis for pass/fail is SPEC transport §1/2/3 and core §2.4. There are two backends: the software USB model of `core-v1` (not a real USB device), and `usb_hw` (pyusb), which opens hardware.

## Running the minimal model

Make explicit settings from `tests/equipment/.env.example`, and give the unit `virtual-core-1`, the SPEC checkout, a new JSON output location and the shared lock file. The model entry point uses only these 4 settings. It does not use ADDRESS/FRAMING/TCP_PEER of the normal entry point and does not connect to hardware. It does not search neighboring checkouts or a private equipment inventory.

```sh
uv run --group dev --env-file tests/equipment/.env \
  python -m oep_client.conformance.usb_model --kind bulk
# Run with another new output location
uv run --group dev --env-file tests/equipment/.env \
  python -m oep_client.conformance.usb_model --kind hid --out /absolute/path/to/new-hid.json
```

The defaults for HID with an ID are report ID 6, input 9 bytes, output 11 bytes. The sizes include the ID, count(u16) and first(u16). With `--report-id 0`, no ID is used, so only the input with another ID is not_applicable, and the reason is kept. `--input-size` / `--output-size` try other sizes. The hardware descriptor is not guessed from these values.

| Check | Stimulus / observation |
|---|---|
| common to bulk/HID (CORE-USB-*) | splitting per byte, joining, length 0, short requests, unknown role, abandonment midway through length/body and recovery (bulk after the gap, HID from the next report with first), oversized length and discarding up to the gap, a request of exactly the declared max_frame |
| HID only (CORE-HID-*) | a report with count 0, ignoring receive padding of any value, discarding input with another report ID, dropping a report whose count exceeds the report capacity and rereading from the next report with first, dropping a frame started when a report goes missing mid-frame (mismatch with the next report's first) and rereading from first, SET_REPORT (normal OUT is also used in all cases) |

A normal request is also sent right after an abnormal count and an oversized length, and it checks that all input before the gap is discarded. The observation for an interruption is the SPEC's frame gap + 100 ms. No response, a single response after recovery, boot/corr, raw transfers/reports, method and time are saved. Only an oversized length with max_frame 65535 cannot be expressed in u16 and is not_applicable. Frame/report corruption and access errors are not hidden by skips or retries.

## Running on hardware

`usb_hw` opens only the one device the user names: the device with VID:PID (when omitted, the project VID:PID in the SPEC's registry) whose serial number equals the unit id (case-insensitive). It takes the common lock before opening, reads the descriptor, and claims the OEP vendor interface for bulk, or the interface with usage page 0xFF4F / usage 0x45 for HID. The shape (bulk's wMaxPacketSize, HID's report length and ID) is taken from the descriptor.

```sh
uv run --group dev python -m oep_client.conformance.usb_hw --kind bulk \
  --unit UNIT --spec /path/to/oep-spec --out /absolute/path/to/new.json --lock /path/to/bench.lock
```

Besides the checks in the table above, the following is checked on hardware only.

| Check | Stimulus / observation |
|---|---|
| USB-DESC-SERIAL / VID-PID | The serial number is the unit id. VID:PID is recorded (the project's, or the one named) |
| USB-DESC-VENDOR / HID | At most one interface with class 0xFF / subclass 0x4F / protocol 0x45, with one pair of bulk IN/OUT. At most one HID with usage page 0xFF4F / usage 0x45, with at most one report ID |
| USB-DESC-WINUSB | MS OS 2.0 compatible ID `WINUSB` (SHOULD. A warning if absent) |
| CORE-USB-IN-END (bulk) | With the source of `oep.probe.link`, makes answers whose frames are exactly 1 and 2 IN packets, and checks they arrive without following data (the end of the probe's last transfer, transports §1). not_applicable if there is no link or max_frame is not enough |

With `--peer hid|bulk` (the other USB transport of the same device) or `--peer-port PATH` (that OEP's CDC port), it opens a second transport and also runs the two-transport checks CORE-ROUTE-PEER-IDENTITY / LOCK / ROUTE (transports §3: the same unit, boot and describe, a different transport number, one lock and holder, answers only on the transport the request came from).

## Contract of the raw adapter

`oep_client.conformance.usb.inspect(factory, ...)` calls the factory after reserving a new output location and taking the shared lock. If the shape is invalid, the factory is not called. The backend is closed at the end and on exceptions, and a close failure FAILs the whole run. On a unit mismatch or an invalid confirm, it does not go on to session operations.

The factory returns a raw backend that opens the endpoint the user named explicitly. The backend's `write(data, method='out'|'set-report')` returns the number of bytes sent, and `read(timeout)` returns the bytes of one transfer/report, and None only on timeout. A bulk bytes length of 0 is a ZLP and is told apart from a timeout. Disconnects, permissions and other USB errors are reported as exceptions. It does not strip or repair ID/count/padding first, and does not resend requests or recover framing.

For the HID shape, pass the report lengths of both directions and the common ID (0 means no ID); for bulk, OUT's wMaxPacketSize. The checker itself encodes and checks HID count/first/ID, and follows a bulk OUT write with a ZLP when it is a multiple of the packet size. The receiving side must ignore any padding, and the probe must send padding as 0. Input stimuli try receiving non-zero padding, and for probe output, non-zero padding, undeclared IDs / oversized count, and a first that points where no frame starts are reported as violations. The rule that a host skips unknown IDs and whether a probe may send undeclared reports are separate checks.

Unplugging/replugging, re-enumeration and power are not checked. Results of using USB with `--framing client` of the normal CLI are message checks only and are not evidence for this independent wire check.

The required `tests/test_conformance_usb.py` verifies the checker with literal bytes, abnormal responses, missing/duplicate data, arbitrary transfer boundaries, the high byte of count, with/without ID, small/large reports and a broken probe model. Success of the model is not treated as success on hardware USB or full core compliance.
