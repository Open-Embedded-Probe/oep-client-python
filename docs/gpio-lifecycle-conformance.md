# Core lifetime checks with a GPIO plan

[日本語](gpio-lifecycle-conformance.ja.md)

`GpioLifecycleChecks(checks, adapter).run()` checks first-stage core rules with an explicit GPIO plan resource.
fn 1 plan, fn 2/3 GPIO, 8 channels, 4 assignments and idle follow the GPIO fixture's contract.
Op codes/tags are read from the selected registry. The public side does not depend on private benches.

`GpioLifecycleModel` is an independent fixture: GpioModel plus an explicit logical ms clock.
Each condition returns to a new boot with an explicit logical restart. Waiting is not approximated with sleep.
Preconditions and conditions are reported as core; the GPIO-specific behavior and the common declarations are checked separately.

- Up to 999 ms of a 1000 ms lease, the pins/plan are kept; at 1000 ms both GPIO plans are undone and all pins return to idle.
- After expiry, the same set/plan request may return the saved success, but it must not reacquire resources, drive, or renew the lease.
  A new set with the old ID is no_session.
- After end, the same ID cannot open anew, with or without force.
- A new open with the same ID (force 0/1) renews the lease and keeps the GPIO plan, the pin state and the resend history.
- A different ID without force gets locked. With force, the previous owner's plan is released and handed to the new owner.
  Old requests do not touch the new owner and get locked; the old success history is not restored even after the new owner ends.
- keepalive and refusal responses within the session keep the GPIO state and renew the lease.
- SID 0 reads and resends of saved responses do not extend the lease and do not re-run set.

The unit tests detect faults such as too-early expiry, missed idle restoration, a lease not renewed, lost history, re-execution after expiry,
release of resources with the same ID, and renewal by SID 0.
Real pin timing, persistent config, the concurrency of deferred writers and force, and the role conditions of other fixtures are not checked.
`full_conformance` is false, and this is not treated as full core / all-standard-op conformance.

```sh
uv run pytest -q tests/test_conformance_gpio_lifecycle.py
```

From the Arduino uv/pytest environment, the path of the public client is also given explicitly.

```sh
uv run --project tests --with-editable /path/to/oep-client-python pytest -c tests/pyproject.toml /path/to/oep-client-python/tests/test_conformance_gpio_lifecycle.py
```
