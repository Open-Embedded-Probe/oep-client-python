# GPIO and plan logical behavior checks

[日本語](gpio-conformance.ja.md)

`GpioChecks(checks, adapter).run()` runs the third-stage checks on an explicit logical fixture.
fn 1 is plan, fn 2/3 are GPIO (instance 0/1). Op codes/tags are read from the selected registry.
External projects can supply observations with their own adapter, but this check's stimuli are limited to the fixture conditions below.
The public code does not depend on an implicit search for private benches or equipment.

The independent GpioModel has 8 channels, GPIO candidates 0 to 7, all 7 modes and 4 simultaneous plan assignments.
Channel 0 idles as output high; the others are inputs. The external input value is the parity of the channel number.
Electrical characteristics are not what the logical model proves.
An explicit logical restart returns to a new boot, and the Core's session/replay mechanism is used.


- When the plan is taken, idle is kept until the first set. Per-fn replacement keeps the other fns and checks conflicts excluding the old assignments of the fn being replaced.
- Plan duplicates / invalid fn / role / channel / capacity / contention are refused before any side effect.
- A GPIO set checks all elements, then drives them in request order. The order of driving the same channel low then high is also observed in the journal.
- set with all 7 modes, and the level read back in output/pull modes. Empty set and read also succeed.
- It checks unassigned channels in set/read (unavailable), undefined modes (unsupported, mark 0 in the fixed part), truncated elements and unknown critical TLVs.
  It matches channel/index/raw tag in the response and checks that plan/pin/journal before and after refusal are exactly the same.
- read on SID 0 keeps the request order and duplicates and does not change the resource state. Extra TLVs in the success response are allowed.
- set and plan_apply on SID 0 are session_required.
- A resend of the same request does not repeat plan application or driving.
- Individual/all release, plan replacement and session end return the old pins to idle.

This fixture's common declarations and GPIO metadata are also run separately.
The unit tests detect faults: wrong wire outcome/reason, reversed order, side effects before refusal, double driving on resend, missed idle restoration.
Physical level/pull/current/waveform, other fixtures' role/channel_group, persistent config, and force/expiry with a GPIO plan are not checked.
This result is not taken as conformance for all standard ops or all plan conditions. `full_conformance` is false.

```sh
uv run pytest -q tests/test_conformance_gpio.py
```

From the Arduino uv/pytest environment, give the path of the public client explicitly.

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_gpio.py
```
