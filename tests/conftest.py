"""The ordinary suite keeps no session files in the user's runtime / cache directory (kept_session): each test gets
its own directory. tests/hw keeps the real one (a run on a real probe is a host run like the CLI's)."""

import pytest


@pytest.fixture(autouse=True)
def _session_dir(request, tmp_path_factory, monkeypatch):
    path = request.node.path
    if path.parent.name != "hw" or path.name == "test_harness.py":
        monkeypatch.setenv("OEP_SESSION_DIR", str(tmp_path_factory.mktemp("sessions")))


# TRANSITIONAL: tests written for oep-spec b80555a that the current spec no longer supports as written. The client
# follows the spec stage by stage (docs/conformance-spec-compatibility.ja.md); each entry is removed when its stage
# rewrites or deletes the test. Keys are node ids or their prefixes.
PENDING_FOLLOW = {
    "tests/test_vectors.py::test_discovery_": "core: discovery moved to test_spec_vectors.py (describe tags renumbered)",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[logic describe: multirate declared]":
        "core: common describe tags renumbered; multirate removed from the spec",
    "tests/test_vectors.py::test_events_as_the_client_reads_them[": "core: event role 0x05 became 0x03",
    "tests/test_vectors.py::test_confirm_": "core: confirm moved to test_spec_vectors.py (no flags)",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[plan_": "plan: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[restart": "restart: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[restart": "restart: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[plan_": "plan: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_refusals_from_the_virtual_bench_byte_for_byte":
        "core: TLV 0x7F is a tag and replay is by the whole request; interface refusals come with their stages",
    "tests/test_vectors.py::test_every_ops_in_the_virtual_bench_and_the_vectors_is_valid":
        "core: ops values now take base rounded to 8 (test_spec_vectors.py checks the current ones)",
    "tests/test_link_serial.py::test_open_host_on_a_serial_port_with_console_bytes_and_tiocexcl":
        "probe.config: bind/slot raw console bytes on the port are gone; a serial transport is OEP frames only (transports §4)",
    "tests/test_request_tails.py::test_a_tag_sent_twice_uses_the_first":
        "fixture uart: a later copy of a known TLV is checked for its value too (core §2.3)",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        why = next((v for k, v in PENDING_FOLLOW.items() if item.nodeid.startswith(k)), None)
        if why:
            item.add_marker(pytest.mark.skip(reason="pending spec follow - " + why))
