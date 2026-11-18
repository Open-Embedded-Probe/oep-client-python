# Minimal setup check

[日本語](hardware-quickstart.ja.md)

Offline checks of equipment TOML and structural role resolution, a virtual smoke that uses OEP messages, and hardware preflight with the common lock are available. The explicit preflight covers a standalone check of a hardware probe. Target writing, wiring discovery and the USB communication contract are not supported. It uses Python 3.10 or later and uv.

From the repository root, you can check with only the bundled settings.

```sh
uv sync --locked
uv run --locked oep-hardware validate --config tests/hw/hardware.example.toml
uv run --locked oep-hardware plan --config tests/hw/hardware.resolved.example.toml --contract USB-DATA
uv run --locked oep-hardware smoke --config tests/fixtures/hardware.virtual.toml
uv run --locked pytest tests/test_hardware.py -q
```

The USB plan assigns `dut` / `peer` to 2 targets of the same probe and shows a plan that requests 2 connections. This does not certify the probe's simultaneous capability or the target's USB API. Pairs that are not connected are not built from the product of all targets.

The virtual smoke checks confirm, list/describe, unit ID, required declarations and session open/end, and records the reported firmware and declarations in JSON. Communication goes through in-process COBS/CRC frames. The result's `scope` shows the limited smoke scope, told apart from hardware conformance or a USB communication guarantee.

## Setting from .env

```sh
cd tests/hw
cp .env.virtual.example .env.virtual
uv run --project ../.. --locked --env-file .env.virtual oep-hardware smoke
uv run --project ../.. --locked --env-file .env.virtual pytest ../equipment -q
```

`.env.virtual` is not tracked by Git. uv loads it explicitly, and the CLI does not search for it. Relative config paths are relative to the CWD at run time. When the CLI's `--config` gives the same item, the CLI wins.

## Saving results

```sh
mkdir -p .pytest-results
uv run --project ../.. --locked --env-file .env.virtual oep-hardware smoke \
  --out .pytest-results/virtual-smoke-1.json
```

Prepare the parent directory of the output location in advance. If the same name exists, it is an error, and past results are not overwritten. A failed smoke is also saved to JSON with exit code 1; settings and I/O errors give exit code 2. Missing equipment in plan is shown as `equipment-unavailable` and is not treated as a passed release gate.

`--probe` narrows the target probes. plan can also use `--target` / `--link` depending on the contract. If an explicitly given target does not exist, or an assignment cannot be made, it is a settings error. The normal virtual smoke checks before any operation that all selected probes are virtual transports, and refuses if even one real transport is included.

## Standalone check of a hardware probe

Copy the public template and prepare a resolved TOML with `example = false`, the real OEP unit ID and the serial/TCP/USB transport filled in. For checking probe declarations only, the targets/wiring tables are not needed. For the common lock, use an existing file the equipment administrator specifies. Copy `.env.example` and set the paths of settings, results and the lock.

```sh
uv run --locked --env-file tests/hw/.env oep-hardware preflight
uv run --locked --env-file tests/hw/.env pytest tests/equipment -q
```

Relative paths are relative to the CWD at run time, so in this example fill in `.env` relative to the repository root. Results are numbered automatically in `OEP_HW_RESULTS`. They can also be given explicitly with `--config`, `--lock` and `--out`. Normal pytest skips the equipment contract without `OEP_HW_CONFIG` and does not open hardware. Given a virtual configuration, the same pytest entry point runs the smoke.

preflight checks only the unit, version in use, declarations and opening/closing a session. It does no target operations or settings saves. It uses the existing firmware and requires no numeric minimum version. A unit mismatch, missing declarations, failure to get a session and cleanup failure are kept in the result and fail. Example configurations, probes with only a manual label, and lock conflicts are refused before any operation. If the result file cannot be reserved, the port is not opened either.

Success of `preflight` does not guarantee exclusivity or conformance of a target test that another process starts later. A target test adapter needs to run the needed preflight and the test together within its own occupied interval.
