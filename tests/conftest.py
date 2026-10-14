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
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[link ": "link: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[gpio ": "gpio: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[UART ": "uart: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[i2c-target": "i2c-target: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[i2c-target": "i2c-target: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[spi-target": "spi-target: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[spi-target": "spi-target: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[UART ": "uart: moved to test_spec_vectors.py",
    "tests/test_config.py::test_uart_item_is_applied_when_the_plan_gives_the_uart_pins":
        "probe.config: the uart item is gone; a UART receives only after a session's configure (fixture §2)",
    "tests/test_config.py::test_a_bind_carries_one_stream_and_keeps_its_position_through_a_session":
        "probe.config: bind is gone; a UART receives only after a session's configure (fixture §2)",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[gpio ": "gpio: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[probe.config set: an idle value of 3 bytes]":
        "probe.config: the idle item is 3 bytes now (channel mode)",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[link ": "link: moved to test_spec_vectors.py",
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
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[rvswd ": "debug: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[riscv-dm ": "debug: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[arm-adi ": "debug: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[console ": "console: its connections are made by the current attach (u32); follows in the console stage",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[rvswd ": "debug: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[riscv-dm ": "debug: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[arm-adi ": "debug: moved to test_spec_vectors.py",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[console ": "console: its connections are made by the current attach (u32); follows in the console stage",
    "tests/test_virtual_bench_spec.py::test_at_boot_slots_attach_and_say_so_without_the_lock":
        "probe.config: slots are gone (connections are session-owned, debug §1, common §2)",
    "tests/test_virtual_bench_spec.py::test_the_seat_rule_closes_the_oldest_slot_only_connection":
        "probe.config: slots are gone; attach never closes a connection to make room (debug §1)",
    "tests/test_virtual_bench_spec.py::test_a_closed_console_stays_readable_and_the_same_place_opens_it_again_under_its_number":
        "console: follows in the console stage (u32 ids, no reopen under the same number)",
    "tests/test_vectors.py::test_ops_vectors_from_the_virtual_bench_byte_for_byte[probe.config": "probe.config: moved to test_spec_vectors.py (label / idle / disable, the generation)",
    "tests/test_vectors.py::test_ops_vectors_as_the_client_sends_and_reads_them[probe.config": "probe.config: moved to test_spec_vectors.py (label / idle / disable, the generation)",
    "tests/test_vectors.py::test_every_wifi_vector_is_routed_to_the_wifi_checks": "probe.config: the wifi item is gone",
    "tests/test_vectors.py::test_a_wifi_set_past_the_probes_max_frame_is_refused_before_sending": "probe.config: the wifi item is gone",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        why = next((v for k, v in PENDING_FOLLOW.items() if item.nodeid.startswith(k)), None)
        if why:
            item.add_marker(pytest.mark.skip(reason="pending spec follow - " + why))
