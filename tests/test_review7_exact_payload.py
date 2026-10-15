"""Review 7 (one class over the whole codebase): checkers and checker-side samples that compared a whole answer or
refusal payload exactly, or took a fixed length as the whole payload, where a09fa45 lets a probe add TLVs.

core §2.3 (a09fa45): 各 op の応答の固定部分（と、前に数か長さを置いた並び）の後ろは TLV の並び。後から足すものはすべてここに TLV で足す。
host は知らない tag を読み飛ばす。
core §4.3 (a09fa45): 表の `—` は固定部分のフィールドが無いことを表す。応答の固定部分の後ろへ TLV を足す §2.3 の規則は、
固定部分が空の成功応答と rejected にも掛かる。知らない応答 TLV は読み飛ばす。
core §4.3 unsupported: `tag(u8)`、[TLV]。 / locked: 残り時間 ms（u32）、[TLV owner（§6.4）]

Each test runs an existing passing checker run while the virtual probe adds an unknown, non-critical TLV (0x7e) after
the fixed part of its refusals (`refusal_tails`) or of its interface answers (`answer_tails`); before the fixes the
checker failed the probe for it.
"""
import importlib

import pytest

from oep_client import virtual_core
from oep_client.virtual_core import tlv

EXTRA = tlv(0x7e, b'x')          # an answer TLV no checker knows (core §2.3: skipped)


@pytest.fixture
def refusal_tails(monkeypatch):
    """Every refusal a virtual_core model raises (core and interfaces alike) carries EXTRA after what it had - the
    model remembers it so (core §5.1), so a replay gives the same bytes."""
    init = virtual_core.Reject.__init__

    def tailed(self, reason, payload=b''):
        init(self, reason, bytes(payload) + EXTRA)
    monkeypatch.setattr(virtual_core.Reject, '__init__', tailed)


@pytest.fixture
def answer_tails(monkeypatch):
    """Every completed answer of an extension (interface or sample) gets EXTRA after its payload, where the frame
    still holds it; the model remembers the answer so (core §5.1). Calling the fixture's value with a predicate on
    (fn, op, fixed part) limits it to those answers."""
    init = virtual_core.Core.__init__
    which = [lambda fn, op, fixed: True]

    def wrap(dispatch, ep):
        def tailed(*args, **kwargs):
            value = dispatch(*args, **kwargs)
            if (isinstance(value, (bytes, bytearray)) and len(value) + 5 + len(EXTRA) <= ep.probe.max_frame
                    and which[0](*args[:3])):
                return virtual_core.Completed(bytes(value) + EXTRA, getattr(value, 'outcome', 0))
            return value
        return tailed

    def core_init(self, endpoint, extension=None):
        init(self, endpoint, extension)
        if extension is not None and not getattr(extension, '_review7_tailed', False):
            for name in ('dispatch_request', 'dispatch'):
                if hasattr(extension, name):
                    setattr(extension, name, wrap(getattr(extension, name), endpoint))
            extension._review7_tailed = True
    monkeypatch.setattr(virtual_core.Core, '__init__', core_init)
    return lambda predicate: which.__setitem__(0, predicate)


def run(module, name, monkeypatch, *args):
    """Runs tests/<module>.py::<name>, with the module's own autouse spec-identity fixture where it has one."""
    mod = importlib.import_module(module)
    checker = module.replace('test_', 'oep_client.', 1)
    if hasattr(importlib.import_module(checker), 'reviewed_identity') and getattr(mod, 'SPEC', None) is not None:
        spec = mod.SPEC
        monkeypatch.setattr(checker + '.reviewed_identity', lambda path: {'commit': 'unit-fixture'} if path == spec else None)
    getattr(mod, name)(*args)


# ---- AttachChecks.refused (every debug wire / target checker built on it): unsupported tag(u8) [TLV] -----------------
ATTACH_FAMILY = [
    ('test_conformance_attach', 'test_standard_attach_and_console_contracts_pass'),
    ('test_conformance_scan', 'test_finite_scan_contracts_pass'),
    ('test_conformance_wire_options', 'test_wire_options_contracts_pass'),
    ('test_conformance_scan_options', 'test_scan_options_contracts_pass'),
    ('test_conformance_mailbox', 'test_logical_mailbox_contracts_pass'),
    ('test_conformance_debug', 'test_mandatory_debug_contracts_pass'),
    ('test_conformance_blocks', 'test_optional_block_contracts_pass'),
    ('test_conformance_step', 'test_optional_step_contracts_pass'),
    ('test_conformance_reset', 'test_optional_reset_contracts_pass'),
    ('test_conformance_run', 'test_optional_run_contracts_pass'),
    ('test_conformance_retry', 'test_retry_contracts_pass'),
    ('test_conformance_cut', 'test_cut_contracts_pass'),
    ('test_conformance_writer_lease', 'test_writer_lease_retains_wire_cut_contracts'),
    ('test_conformance_arm_adi', 'test_arm_contracts_pass'),
    ('test_conformance_arm_adi_blocks', 'test_mem_ap_packet_contracts_pass'),
]


@pytest.mark.parametrize('module,name', ATTACH_FAMILY, ids=[m.split('_', 2)[2] for m, _ in ATTACH_FAMILY])
def test_debug_checkers_accept_tlvs_after_a_refusal(refusal_tails, monkeypatch, module, name):
    run(module, name, monkeypatch)


# ---- LogicChecks.refused (logic, analog): unsupported tag [TLV], unavailable's TLVs in any number --------------------
@pytest.mark.parametrize('module,name', [('test_conformance_logic', 'test_logic_contract'),
                                         ('test_conformance_analog', 'test_analog_operations')], ids=['logic', 'analog'])
def test_capture_checkers_accept_tlvs_after_a_refusal(refusal_tails, monkeypatch, module, name):
    run(module, name, monkeypatch)


# ---- restart §2 TLV case: unsupported's tag, then TLVs ---------------------------------------------------------------
def test_restart_checker_accepts_tlvs_after_the_unsupported_tag(refusal_tails, monkeypatch):
    run('test_conformance_restart', 'test_restart_contract', monkeypatch)


# ---- capture serial: read / segments / release answers and the cause-6 refusal of a stale generation ----------------
def test_capture_serial_checker_accepts_answer_and_refusal_tails(refusal_tails, answer_tails, monkeypatch):
    run('test_conformance_capture_serial', 'test_capture_serial_wrap_and_release', monkeypatch)


# ---- core refusal order / response race / probe input / TCP recovery / processing order: whole frames compared -----
@pytest.mark.parametrize('module,name', [
    ('test_conformance_refusal_order', 'test_refusal_precedence_matrix'),
    ('test_conformance_response_race', 'test_competing_writer_completion'),
    ('test_conformance_probe_input', 'test_raw_probe_role_and_header_rules'),
    ('test_conformance_tcp_recovery', 'test_tcp_recovery'),
    ('test_conformance_execution_order', 'test_dependent_effects_pass_for_every_writer_state'),
], ids=['refusal-order', 'response-race', 'probe-input', 'tcp-recovery', 'execution-order'])
def test_core_checkers_accept_tlvs_after_a_refusal(refusal_tails, monkeypatch, module, name):
    run(module, name, monkeypatch)


# ---- plan_apply / gpio set answer — (the link and restart samples' seed) ---------------------------------------------
@pytest.mark.parametrize('module,name,args', [
    ('test_conformance_link_transfers', 'test_link_transfer_profiles', (64,)),
    ('test_conformance_restart', 'test_restart_contract', ()),
], ids=['link', 'restart'])
def test_samples_seed_plan_and_gpio_whatever_tlvs_follow(answer_tails, monkeypatch, module, name, args):
    answer_tails(lambda fn, op, fixed: (fn, op) in ((1, 16), (2, 16)))          # plan_apply, gpio set: — [TLV]
    run(module, name, monkeypatch, *args)


# ---- the sample extensions' answers (an extension follows core §2.3 like any op) -----------------------------------
SAMPLE_USERS = [
    ('test_conformance_resources', 'test_resource_contracts_pass_independent_minimal_extension', ()),
    ('test_conformance_retention', 'test_retention_passes', ()),
    ('test_conformance_corr', 'test_corr_boundaries_pass', ()),
    ('test_conformance_session_loss', 'test_session_loss_passes', ()),
    ('test_conformance_retention_lifecycle', 'test_lifecycle_passes', ()),
    ('test_conformance_replay_pressure', 'test_pressure_replay', ()),
    ('test_conformance_recovery', 'test_recovery_passes', ()),
    ('test_conformance_exhausted', 'test_exhausted_recovery_passes', ('prefix',)),
    ('test_conformance_recovery_races', 'test_races_pass', ('prefix',)),
    ('test_conformance_routes', 'test_routes_pass', ()),
    ('test_conformance_notification_sequence', 'test_notification_sequence_contracts_pass', ()),
    ('test_conformance_host_resend', 'test_same_byte_resends_and_probe_cache', ()),
    ('test_conformance_tlv_requests', 'test_tlv_request_contracts_pass', ()),
    ('test_conformance_response_race', 'test_competing_writer_completion', ()),
    ('test_conformance_probe_input', 'test_raw_probe_role_and_header_rules', ()),
    ('test_conformance_tcp_recovery', 'test_tcp_recovery', ()),
    ('test_conformance_execution_order', 'test_dependent_effects_pass_for_every_writer_state', ()),
]


@pytest.mark.parametrize('module,name,args', SAMPLE_USERS, ids=[m.split('_', 2)[2] for m, _, _ in SAMPLE_USERS])
def test_sample_extension_readers_skip_an_answer_tail(answer_tails, monkeypatch, module, name, args):
    # not a create the retention checks size on purpose (its reply_size byte other than 7): those test the probe's
    # remembered-answer bound at an exact length (core §5.1)
    answer_tails(lambda fn, op, fixed: not (op == 16 and len(fixed) > 1 and fixed[1] != 7))
    run(module, name, monkeypatch, *args)
