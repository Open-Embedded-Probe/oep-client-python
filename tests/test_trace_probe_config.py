"""oep.probe.config (oep-spec a09fa45 interfaces/oep-if-probe-config.ja.md) on the virtual bench: the rules
tests/test_config*.py and the config checkers leave to the trace - a label's text length, a label leaving the
firmware's describe alone, an idle set while the channel is in use, count 0 and a critical tail, save past
storage, save / erase leaving the settings, and the table of results after a save or an erase (§3)."""

import struct

import pytest

from oep_client import config, core, fixture, host as h, message as m

from test_config import open_bench

CAUSE = 1                                                    # unavailable's cause TLV (core §4.3)


def state(cfg):
    s = cfg.state()
    return s.storage, s.saved_generation, s.reason


def label(channel, text: bytes) -> bytes:
    return struct.pack("<BH", config.ITEM["label"], 2 + len(text)) + struct.pack("<H", channel) + text


def test_a_labels_text_is_1_to_32_bytes():
    """probe-config §1 label: channel(u16) text of 1 to 32 bytes; another length is malformed and changes nothing."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    for text in (b"", b"x" * 33):
        with pytest.raises(h.Rejected) as e:
            hst.call(cfg.fn, cfg.SET, b"\x01" + label(20, text))
        assert e.value.result.detail == m.MALFORMED
    assert cfg.get() == (1, [])
    for text in (b"x", b"y" * 32):
        hst.call(cfg.fn, cfg.SET, b"\x01" + label(20, text))
        assert cfg.items() == [config.Label(channel=20, text=text.decode())]


def test_a_label_is_the_settings_name_and_leaves_the_firmwares_alone():
    """probe-config §1 label: a setting's name - describe's firmware label (core §7.5) is unchanged, and the name moves
    no line (no plan, pin or connection changes)."""
    ep, hst = open_bench()
    before = (core.describe(hst, 0), set(ep.plan), dict(ep.parked), dict(ep.gpio_modes), dict(ep.conns))
    config.ProbeConfig(hst).set([config.Label(channel=20, text="A SWDIO"), config.Label(channel=2, text="power_hi")])
    assert (core.describe(hst, 0), set(ep.plan), dict(ep.parked), dict(ep.gpio_modes), dict(ep.conns)) == before
    assert ep.static_labels[2] == "A SWDIO"


def test_an_idle_set_while_the_channel_is_in_use_applies_when_it_is_free():
    """probe-config §1 idle: a change to a free channel applies at once; to one in use, the next time it is free."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    gpio_fn = ep.fns["oep.fixture.gpio"]
    core.plan_apply(hst, [(gpio_fn, 1, 20)])
    gpio = fixture.Gpio(hst, gpio_fn)
    gpio.set([(20, gpio.OUTPUT_LOW)])
    parked = ep.parked.get(20)
    cfg.set([config.Idle(channel=20, mode="output-high"), config.Idle(channel=21, mode="pull-down")])
    assert ep.parked[21] == config.IDLE["pull-down"]                          # free: at once
    assert ep.gpio_modes[20] == gpio.OUTPUT_LOW and ep.parked.get(20) == parked and gpio.read([20]) == [0]   # in use
    core.plan_release(hst, [gpio_fn])
    assert ep.parked[20] == config.IDLE["output-high"]                        # free again: the new idle


def test_count_0_is_a_no_op_and_a_critical_tail_is_refused_before_anything():
    """probe-config §2: a set / unset of count 0 succeeds with the generation unchanged; an unknown critical TLV after
    the items is refused (unsupported, its tag) before any change (core §2.3), a non-critical one ignored."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=21, mode="pull-up")])
    before = (cfg.get(), dict(ep.parked))
    for op in (cfg.SET, cfg.UNSET):
        assert struct.unpack("<I", hst.call(cfg.fn, op, b"\x00").payload[:4])[0] == 2
    assert (cfg.get(), dict(ep.parked)) == before
    item = config.item(config.Disable(channel=30))
    with pytest.raises(h.Unsupported) as e:
        hst.call(cfg.fn, cfg.SET, b"\x01" + item + m.tlv(0x3D, b"\x01", critical=True))
    assert e.value.tag == 0x3D | m.TAG_CRITICAL and (cfg.get(), dict(ep.parked)) == before
    with pytest.raises(h.Unsupported):
        hst.call(cfg.fn, cfg.UNSET, b"\x01" + struct.pack("<BH", config.ITEM["idle"], 21) + m.tlv(0x3D, b"", critical=True))
    assert (cfg.get(), dict(ep.parked)) == before
    assert struct.unpack("<I", hst.call(cfg.fn, cfg.SET, b"\x01" + item + m.tlv(0x3D, b"\x01")).payload[:4])[0] == 3


def test_a_save_past_storage_is_cause_3_before_anything():
    """probe-config §2 / §4: a change the probe cannot keep may be refused cause 2/3; a save past storage's max_bytes
    (items with their headers) is unavailable cause 3 before any write - nothing saved, the settings kept."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Label(channel=20, text="x" * 10)])                        # 3 + 2 + 10 = 15 bytes
    ep.storage_max = 15
    assert cfg.save() == 2 and state(cfg) == ("applied", 2, None)
    cfg.set([config.Label(channel=21, text="y")])
    with pytest.raises(h.Unavailable) as e:
        cfg.save()
    assert dict(e.value.tlvs)[CAUSE] == b"\x03" and state(cfg) == ("applied", 2, None) and cfg.get()[0] == 3
    ep.reboot()
    assert config.ProbeConfig(hst).items() == [config.Label(channel=20, text="x" * 10)]


def test_save_and_erase_leave_the_settings_the_generation_and_the_pins():
    """probe-config §3: save / erase change neither the settings, the generation nor the pins, succeeded or failed;
    erase removes only the saved copy and succeeds with nothing saved."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=21, mode="output-high"), config.Disable(channel=30)])
    now = lambda: (cfg.get(), dict(ep.parked), ep.disabled)
    before = now()
    cfg.erase()                                                               # nothing saved yet: success
    assert now() == before and state(cfg) == ("none", 0, None)
    cfg.save()
    assert now() == before
    for fault in ("old", "none", "unreadable"):
        ep.storage_fault = fault
        with pytest.raises(h.Failed):
            cfg.save()
        assert now() == before
    cfg.erase()
    assert now() == before and ep.saved is None


@pytest.mark.parametrize("op, fault, after", [
    ("save", None, ("applied", "current", None)),
    ("erase", None, ("none", 0, None)),
    ("save", "old", "before"), ("erase", "old", "before"),
    ("save", "none", ("none", 0, None)), ("erase", "none", ("none", 0, None)),
    ("save", "unreadable", ("unreadable", 0, "form")), ("erase", "unreadable", ("unreadable", 0, "form")),
])
def test_the_results_table_of_save_and_erase(op, fault, after):
    """probe-config §3 (the results of a storage op): success - save 1 / the generation / 0, erase 0 / 0 / 0; failed
    (completed failed, never partial, the same fixed part as success) with the old save kept - the values before;
    with none - 0 / 0 / 0; unreadable - 2 / 0 / 1."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Label(channel=20, text="a")])
    cfg.save()
    cfg.set([config.Label(channel=20, text="b")])
    before = state(cfg)
    assert before == ("applied", 2, None) and cfg.get()[0] == 3
    ep.storage_fault = fault
    r = hst.request(cfg.fn, cfg.SAVE if op == "save" else cfg.ERASE)
    assert r.resolution == m.COMPLETED and r.detail == (m.SUCCESS if fault is None else m.FAILED)
    assert r.payload == (struct.pack("<I", 3) if op == "save" else b"")
    want = before if after == "before" else tuple(3 if v == "current" else v for v in after)
    assert state(cfg) == want


def test_the_interface_row_lists_oep_probe_config_at_revision_1():
    """probe-config preamble (the interface row): the probe lists oep.probe.config by that name at revision 1."""
    from oep_client import registry as reg
    ep, hst = open_bench()
    entries = [e for e in core.list_entries(hst) if e.name == "oep.probe.config"]
    assert [(e.fn, e.revision) for e in entries] == [(ep.fns["oep.probe.config"], 1)]
    assert reg.PROBE_CONFIG.revision == 1


@pytest.mark.parametrize("op, payload, locked", [
    ("get", b"\x00\x00", False), ("set", b"\x01" + config.item(config.Disable(channel=30)), True), ("save", b"", True),
    ("erase", b"", True), ("unset", b"\x01" + struct.pack("<BH", config.ITEM["idle"], 21), True), ("state", b"", False)],
    ids=["get", "set", "save", "erase", "unset", "state"])
def test_the_lock_column_of_the_op_table(op, payload, locked):
    """probe-config §2 (the op table, lock column): get and state need no lock (answered with session_id 0); set,
    save, erase and unset need it - session_required with 0, and the settings, the generation and the saved copy stay."""
    from oep_client import registry as reg
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=21, mode="pull-up")])
    cfg.save()
    before = (cfg.get(), state(cfg), dict(ep.saved), ep.disabled)
    code = reg.PROBE_CONFIG.op[op]
    assert (code not in reg.PROBE_CONFIG.lock_free) == locked
    r = m.Result.unpack(ep.handle(m.Request(hst.next_corr(), cfg.fn, code, payload, 0).pack(), 1))
    if locked:
        assert r.detail == m.SESSION_REQUIRED
        assert (cfg.get(), state(cfg), dict(ep.saved), ep.disabled) == before
    else:
        assert r.succeeded


def test_unset_ignores_bit_7_of_the_item_tag():
    """probe-config §2: an item tag's bit 7 is treated in unset as in set - unset of (0x83, channel) removes the idle
    item (0x03, channel) and moves the generation; the same key once more is a no-op."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    g = cfg.set([config.Idle(channel=21, mode="pull-up"), config.Label(channel=21, text="x")])
    r = hst.call(cfg.fn, cfg.UNSET, b"\x01" + struct.pack("<BH", config.ITEM["idle"] | 0x80, 21))
    assert struct.unpack_from("<I", r.payload)[0] == g + 1
    assert cfg.items() == [config.Label(channel=21, text="x")]
    r = hst.call(cfg.fn, cfg.UNSET, b"\x01" + struct.pack("<BH", config.ITEM["idle"] | 0x80, 21))
    assert struct.unpack_from("<I", r.payload)[0] == g + 1


def test_save_and_erase_complete_within_max_op_ms():
    """probe-config §3: the whole save / erase finishes inside the core's max_op_ms - the virtual bench answers each
    completed in the request itself (never accepted / deferred), on a clock that has not moved past max_op_ms."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    limit = core.max_op_ms(hst)
    assert limit >= 1
    cfg.set([config.Label(channel=20, text="a" * 32)])
    start = ep.now()
    for op in (cfg.SAVE, cfg.ERASE):
        r = hst.request(cfg.fn, op)
        assert r.resolution == m.COMPLETED and r.detail == m.SUCCESS and ep.now() - start <= limit


# ---- second review additions ---------------------------------------------------------------------------------------

def test_a_key_twice_or_a_wrong_length_stays_malformed_beside_an_idle_disable_conflict():
    """probe-config §1 disable: idle and disable on one channel is unavailable cause 5, but a set that also repeats a key
    or carries an item of the wrong length is still malformed - the form is refused first, nothing changes."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=40, mode="pull-up")])
    before = (cfg.get(), dict(ep.parked), set(ep.disabled))
    dis = config.item(config.Disable(channel=40))
    for body in (b"\x02" + dis * 2,                                                    # the conflicting key twice
                 b"\x02" + dis + m.tlv(config.ITEM["disable"], b"\x29")):              # and a 1-byte disable
        with pytest.raises(h.Rejected) as e:
            hst.call(cfg.fn, cfg.SET, body)
        assert e.value.result.detail == m.MALFORMED
        assert (cfg.get(), dict(ep.parked), set(ep.disabled)) == before
    with pytest.raises(h.Unavailable) as e:
        hst.call(cfg.fn, cfg.SET, b"\x01" + dis)                                       # alone: the conflict, cause 5
    assert dict(e.value.tlvs)[CAUSE] == b"\x05"


def test_a_refused_or_unchanging_set_touches_no_pin(monkeypatch):
    """probe-config §2: a refusal keeps the settings, the generation and the pins; a set / unset that changes nothing
    does not set any pin again (the virtual bench parks nothing)."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=21, mode="pull-up")])
    core.plan_apply(hst, [(4, 1, 30)])
    parks = []
    park = ep._park
    monkeypatch.setattr(ep, "_park", lambda chs: (parks.append(set(chs)), park(chs)))
    before = (cfg.get(), dict(ep.parked))
    with pytest.raises(h.Unavailable):
        cfg.set([config.Idle(channel=22, mode="output-high"), config.Disable(channel=30)])   # 30 in use: cause 1
    cfg.set([config.Idle(channel=21, mode="pull-up")])                                   # the same contents
    cfg.unset([("disable", 23)])                                                        # a key not there
    hst.call(cfg.fn, cfg.SET, b"\x00")
    assert parks == [] and (cfg.get(), dict(ep.parked)) == before
    cfg.set([config.Idle(channel=22, mode="pull-down")])
    assert parks == [{22}] and ep.parked[22] == config.IDLE["pull-down"]
