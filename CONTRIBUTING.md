# Contributing

[日本語](CONTRIBUTING.ja.md)

## Tests

```sh
uv run pytest                                                            # the virtual bench (no hardware)
OEP_HW_BOARDS=<board id> OEP_HW_NOFLASH=1 uv run pytest tests/hw -m hw   # hardware: tests/hw/README.md
```

The minimal setup check is the [setup check steps](docs/hardware-quickstart.md). See also the common
[testing policy](docs/testing-policy.md) and the [guide for projects that use OEP](docs/testing-consumers.md). Equipment settings are in
the [configuration design](docs/hardware-configuration.md) and the [settings format and templates](docs/hardware-schema.md). Conformance
checks from the spec are in [Conformance checks from the SPEC](docs/conformance-checks.md).

## Number tables and shared vectors

Numbers are taken from `oep_client.registry`, a straight copy of oep-spec's `generated/oep-v1/oep_v1_registry.py`. The number tables and
oep-spec's test vectors (`tests/vectors/*.json`; `tests/test_spec_vectors.py` matches this client and the virtual bench against them)
are copied from a named oep-spec checkout with `OEP_SPEC_DIR=<checkout> tools/sync_registry.sh` (`OEP_SPEC_REF` chooses a commit other
than HEAD; the commit and the file hashes stay in `tests/vectors/SPEC_COMMIT` / `SPEC_SHA256`).

## Documents

Documents are changed in Japanese (`.ja.md`) and English (`.md`) in the same change. Until oep-spec's freeze (`v1.0.0`) the Japanese is
authoritative. Documents describe the current state only; the history of changes goes in CHANGELOG.md.

## Releases

Releases are the GitHub Actions Release (workflow_dispatch). Pick version_level (patch / minor / major, patch by default) and
`tools/prepare_release.py` bumps that part of the version in pyproject.toml (or give version = X.Y.Z or X.Y.ZbN for that one). It
rewrites pyproject.toml, uv.lock and `oep_client.__version__`, turns CHANGELOG.md's Unreleased into that version, and after the tests and
build it commits and tags, and publishes the GitHub Release and to PyPI (Trusted Publishing). Add changes to CHANGELOG.md's Unreleased in
(EN) / (JA) beforehand.
