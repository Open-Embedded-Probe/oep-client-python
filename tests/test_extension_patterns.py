"""How an application that only shares OEP's core (and the project's USB ID, PID-USE.md) defines its own interface, and
how others extend it, under oep-spec e49550d (core §2.2, §2.5, §2.8, §7.2, §7.7, §12 rule 7, §12.1).

The application is a thermometer that is no probe of pins: its maker A defines `io.example.thermo` revision 1. Then:

- A itself adds an optional op and an answer TLV in revision 1, and later changes a fixed form in revision 2;
- B (com.b) adds information to A's answer and an option to A's request by named extensions, a sensor of its own to A's
  named list, and a new op in its own interface `com.b.thermo-calib`;
- C (org.c) extends B's interface the same way, and adds to A's answer next to B.

Each test is one pattern, run on the virtual core (virtual_core.Core) with the hosts each party would write: a host
that knows only A's revision 1, one that also knows B's names, and so on. Two tests show the alternatives the rules
forbid - a number another party took, a non-critical extension that changes the meaning of a fixed field - going
wrong. Every protocol number is looked up by name: the core's from the registry, A's / B's / C's from their own tables
below (each interface's own registry, as its owner would publish it)."""

import copy
import struct
import time

import pytest

from oep_client import core, host as h, message as m
from oep_client import registry as reg
from oep_client.conformance import Checks
from oep_client.endpoint import Endpoint
from oep_client.virtual_bench import core_v1, with_unit_id
from oep_client.virtual_core import Core, Reject
from oep_client.virtual_names import NAMED_EXTENSION, R
from test_conformance import REG

DESCRIBE_OPS = reg.DESCRIBE_COMMON["ops"]
UNKNOWN_OPERATION = reg.REJECT_REASONS["unknown_operation"]

# A's registry for io.example.thermo (its owner's numbers, core §2.5 rule 1)
THERMO = "io.example.thermo"
T_OP = {"read": 0x10, "set_interval": 0x11, "history": 0x12}           # history: A's later optional op (revision 1)
T_READ_ANSWER = {"timestamp_ns": 0x01, "battery_mv": 0x02}              # A's later optional answer TLVs
T_DESCRIBE = {"sensor": 0x40, "interval_range": 0x41}                   # sensor: a named list (core §7.7)
T_SENSOR = ("board", "ambient")                                         # A's standard sensor names
# B's and C's names (reverse-DNS, core §12 rule 1)
B_HUMIDITY, B_AVERAGE, B_TIP = b"com.b.humidity", b"com.b.average", "com.b.probe-tip"
CALIB = "com.b.thermo-calib"
C_OP = {"calibrate": 0x10}                                              # B's registry for its own interface
C_HUMIDITY, C_CERT = b"org.c.humidity", b"org.c.cert"


def named(name: bytes, data: bytes, critical: bool = False) -> bytes:
    """A named extension TLV (core §2.2): name_len name data."""
    return m.tlv(NAMED_EXTENSION, bytes((len(name),)) + name + data, critical=critical)


def extensions(tail: bytes) -> dict[bytes, bytes]:
    """The named extensions in an answer's TLVs, by name (the first of each)."""
    out = {}
    for tag, value in m.split_tlvs(tail):
        if tag & 0x7F == NAMED_EXTENSION:
            out.setdefault(value[1:1 + value[0]], value[1 + value[0]:])
    return out


class App:
    """The thermometer's interfaces on the virtual core. `parts` says what this firmware has:
    'history', 'timestamp' (A's optional additions), 'battery' (A's battery_mv TLV), 'rev2' (A's revision 2 next to 1),
    'only_rev2', 'b_humidity', 'b_average', 'b_tip', 'calib' (B), 'c_humidity', 'c_cert' (C), 'b_raw_humidity' (B
    taking A's number 0x02 for humidity - what rule 7 forbids), 'b_fahrenheit' (B's non-critical extension that turns
    the reading into Fahrenheit - what §12 rule 7's last line forbids)."""

    def __init__(self, parts=()):
        self.parts = set(parts)
        self.interval = 1000
        self.offset = {}                                   # sensor name -> calibration offset (m°C)
        self.sensors = list(T_SENSOR)
        if "b_tip" in self.parts:
            self.sensors.insert(0, B_TIP)                  # B lists its sensor first: the standard ones move
        self.milli = {"board": 31500, "ambient": 22750, B_TIP: 85125}
        rows = []                                          # (fn, name, instance, revision)
        if "only_rev2" not in self.parts:
            rows.append((1, THERMO, 0, 1))
        if "rev2" in self.parts or "only_rev2" in self.parts:
            rows.append((2, THERMO, 0, 2))
        if "calib" in self.parts:
            rows.append((3, CALIB, 0, 1))
        self.rows = rows
        self.functions = tuple(r[0] for r in rows)

    # ---- the core's hooks (virtual_core.Core) ----
    def release(self):
        pass

    def kind(self, fn):
        return next((name, rev) for f, name, _, rev in self.rows if f == fn)

    def fixed_for(self, fn):
        name, rev = self.kind(fn)
        if name == CALIB:
            return {C_OP["calibrate"]: 0}
        ops = {T_OP["read"]: 0, T_OP["set_interval"]: 0}
        if "history" in self.parts:
            ops[T_OP["history"]] = 0
        return ops

    def free_for(self, fn):
        name, rev = self.kind(fn)
        return set() if name == CALIB else {T_OP["read"], T_OP["history"]}

    def list_page(self, first, limit):
        out = [struct.pack("<HHBB", fn, inst, rev, len(name)) + name.encode() for fn, name, inst, rev in self.rows]
        page = bytearray(struct.pack("<HB", len(out), 0))
        for row in out[first:]:
            if len(page) + len(row) > limit:
                break
            page += row
            page[2] += 1
        return bytes(page)

    def describe(self, fn):
        name, rev = self.kind(fn)
        ops = sorted(self.fixed_for(fn))
        bitmap = sum(1 << (op - ops[0]) for op in ops)
        rows = [m.tlv(DESCRIBE_OPS, bytes((ops[0],)) + bitmap.to_bytes(1, "little"))]
        if name == THERMO:
            rows += [m.tlv(T_DESCRIBE["sensor"], s.encode()) for s in self.sensors]
            rows.append(m.tlv(T_DESCRIBE["interval_range"], struct.pack("<II", 100, 60000)))
            if "b_humidity" in self.parts:                 # B's describe addition: which sensors measure humidity
                rows.append(named(B_HUMIDITY, bytes((self.sensors.index("ambient") + 1,))))
        return tuple(rows)

    def request_tags(self, fn, op, data):
        return data                                        # the whole payload: each interface parses its own

    def options(self, tail, known):
        """The request's TLVs (core §2.2/§2.3): a named extension this firmware knows -> its data; an unknown name
        ignored, or refused unsupported naming 0xFF when critical; another unknown critical tag refused likewise."""
        got = {}
        try:
            tlvs = m.split_tlvs(tail)
        except m.ProtocolError:
            raise Reject(R["malformed"]) from None
        for tag, value in tlvs:
            if tag & 0x7F == NAMED_EXTENSION:
                name = value[1:1 + value[0]]
                if name in known:
                    got[name] = value[1 + value[0]:]
                elif tag & m.TAG_CRITICAL:
                    raise Reject(R["unsupported"], bytes((tag,)))
            elif tag & m.TAG_CRITICAL:
                raise Reject(R["unsupported"], bytes((tag,)))
        return got

    def dispatch_request(self, fn, op, fixed, payload):
        name, rev = self.kind(fn)
        if name == CALIB:
            return self.calibrate(payload)
        if op == T_OP["read"]:
            known = {B_AVERAGE} if "b_average" in self.parts else set()
            if "b_fahrenheit" in self.parts:
                known.add(b"com.b.fahrenheit")
            opts = self.options(payload, known)
            return self.read(rev, b"com.b.fahrenheit" in opts)
        if op == T_OP["set_interval"]:
            if len(payload) < 4:
                raise Reject(R["malformed"])
            interval = struct.unpack_from("<I", payload)[0]
            opts = self.options(payload[4:], {B_AVERAGE} if "b_average" in self.parts else set())
            if not 100 <= interval <= 60000:
                raise Reject(R["unsupported"], b"\0")
            self.interval = interval
            self.average = opts.get(B_AVERAGE, b"\x01")[0]
            return struct.pack("<I", interval)
        if op == T_OP["history"]:
            return b"\0"                                   # count 0: nothing kept yet
        raise Reject(UNKNOWN_OPERATION)

    def read(self, rev, fahrenheit=False):
        """read -> count(u8), count x (sensor u8, value i32 m°C) in revision 1, (sensor u8, value i64 µ°C) in 2."""
        rows = b""
        for k, s in enumerate(self.sensors, 1):
            v = self.milli[s] + self.offset.get(s, 0)
            if fahrenheit:
                v = v * 9 // 5 + 32000
            rows += struct.pack("<Bi", k, v) if rev == 1 else struct.pack("<Bq", k, v * 1000)
        out = bytes((len(self.sensors),)) + rows
        if "timestamp" in self.parts:
            out += m.tlv(T_READ_ANSWER["timestamp_ns"], struct.pack("<Q", 123_000_000))
        if "battery" in self.parts:
            out += m.tlv(T_READ_ANSWER["battery_mv"], struct.pack("<H", 3300))
        if "b_raw_humidity" in self.parts:                 # B took A's number (forbidden): permille of sensor 2
            out += m.tlv(T_READ_ANSWER["battery_mv"], struct.pack("<BH", 2, 451))
        if "b_humidity" in self.parts:                     # B's own name: (sensor, permille) pairs
            out += named(B_HUMIDITY, struct.pack("<BH", self.sensors.index("ambient") + 1, 451))
        if "c_humidity" in self.parts:                     # C's own name, its own form (a percent u8)
            out += named(C_HUMIDITY, bytes((45,)))
        return out

    def calibrate(self, payload):
        """com.b.thermo-calib calibrate: thermo_fn(u16), sensor(u8, the number in that fn's list), offset(i32) [TLV]
        -> applied offset(i32) [TLV]."""
        if len(payload) < 7:
            raise Reject(R["malformed"])
        fn, sensor, offset = struct.unpack_from("<HBi", payload)
        self.options(payload[7:], set())
        if fn not in self.functions or self.kind(fn)[0] != THERMO or not 1 <= sensor <= len(self.sensors):
            raise Reject(R["unsupported"], b"\0")
        self.offset[self.sensors[sensor - 1]] = offset
        out = struct.pack("<i", offset)
        if "c_cert" in self.parts:                         # C's addition to B's answer
            out += named(C_CERT, b"CAL-2026-0042")
        return out


def device(*parts, unit="thermo-1"):
    """The application on the virtual core: (endpoint send, the app)."""
    app = App(parts)
    origin = time.monotonic_ns()
    ep = Endpoint(with_unit_id(core_v1(), unit), lambda: (time.monotonic_ns() - origin) // 1_000_000, boot_id=7)
    ep.current_core = Core(ep, app)
    return ep.handle, app


def connect(*parts):
    send, app = device(*parts)
    hst = h.Host(send)
    hst.open(3000)
    return hst, app


# ---- the hosts each party writes ----------------------------------------------------------------------------------

class ThermoHost:
    """A host for io.example.thermo revision 1: finds the fn by (name, revision), the sensors by name."""
    REVISION = 1

    def __init__(self, hst):
        self.hst = hst
        self.fn = core.find(hst, THERMO, self.REVISION)
        self.sensors = [v.decode() for t, v in core.describe(hst, self.fn) if t == T_DESCRIBE["sensor"]]

    def number(self, sensor):
        return self.sensors.index(sensor) + 1             # the place from 1 (core §7.7)

    def read_raw(self, tail=b""):
        return self.hst.call(self.fn, T_OP["read"], tail, locked=False).payload

    def read(self, tail=b"") -> dict[str, float]:
        p = self.read_raw(tail)
        rows = {self.sensors[p[1 + 5 * i] - 1]: struct.unpack_from("<i", p, 2 + 5 * i)[0] / 1000 for i in range(p[0])}
        self.tail = p[1 + 5 * p[0]:]                       # TLVs after the rows: skipped unless known (core §2.2)
        return rows


class BHost(ThermoHost):
    """B's host: A's revision 1 plus B's names."""

    def humidity(self) -> dict[str, float]:
        self.read()
        data = extensions(self.tail).get(B_HUMIDITY)
        if data is None:
            return {}
        return {self.sensors[data[i] - 1]: struct.unpack_from("<H", data, i + 1)[0] / 10 for i in range(0, len(data), 3)}


# ---- the patterns ---------------------------------------------------------------------------------------------------

def test_an_application_with_only_the_core_passes_the_core_checks():
    """A device that is no probe of pins (no channels, no plan, no oep. interface) still speaks OEP: confirm, list,
    describe, clock and the session (core §7, §6) - the PID-USE.md case. The core checker passes it as it is."""
    send, app = device()
    report = Checks(send, copy.deepcopy(REG), "thermo-1").run()
    assert report["status"] == "passed", [(x["id"], x.get("error")) for x in report["checks"] if x["status"] != "passed"]
    hst = h.Host(send)
    assert [e.name for e in core.list_entries(hst)] == [THERMO]
    assert core.unit_id(hst) == "thermo-1" and not [t for t, v in core.describe(hst, 0) if t == reg.CORE.tlv["describe"]["channels"]]


def test_the_owner_adds_an_optional_op_and_an_answer_tlv_in_the_same_revision():
    """core §2.8 / §12.1: A adds `history` (declared in ops) and `timestamp_ns` to read's answer without a new
    revision. A host of the first release reads the new firmware unchanged (the TLV skipped); a newer host asks ops
    before calling history, and an old firmware refuses it as an unknown operation."""
    hst, app = connect("history", "timestamp")
    old = ThermoHost(hst)
    assert old.read() == {"board": 31.5, "ambient": 22.75}
    assert core.offers(hst, old.fn, T_OP["history"])
    hst2, _ = connect()
    assert not core.offers(hst2, ThermoHost(hst2).fn, T_OP["history"])
    with pytest.raises(h.Rejected) as e:
        hst2.call(ThermoHost(hst2).fn, T_OP["history"], b"", locked=False)
    assert e.value.result.detail == UNKNOWN_OPERATION


def test_another_party_adds_information_by_a_named_extension():
    """core §2.2 / §12 rule 7: B adds humidity to A's read answer (and to describe) under its name; C adds its own,
    in another form, beside it. A's host reads what it always did; B's host reads B's and not C's; no number was taken
    in A's space, so A's later answer TLVs cannot collide with either."""
    hst, app = connect("b_humidity", "c_humidity", "battery")
    assert ThermoHost(hst).read() == {"board": 31.5, "ambient": 22.75}
    b = BHost(hst)
    assert b.humidity() == {"ambient": 45.1}
    assert set(extensions(b.tail)) == {B_HUMIDITY, C_HUMIDITY}
    assert dict(m.split_tlvs(b.tail))[T_READ_ANSWER["battery_mv"]] == struct.pack("<H", 3300)   # A's own, untouched


@pytest.mark.parametrize("firmware_knows", [False, True])
def test_another_party_adds_a_request_option(firmware_knows):
    """core §2.2: B's averaging window on A's set_interval. Sent critical, a firmware without B's name refuses it
    unsupported naming 0xFF and changes nothing; with it, it applies. A non-critical name it does not know is ignored -
    so only an option the request means the same without may be sent non-critical."""
    hst, app = connect(*(("b_average",) if firmware_knows else ()))
    t = ThermoHost(hst)
    body = struct.pack("<I", 500) + named(B_AVERAGE, b"\x04", critical=True)
    if firmware_knows:
        hst.call(t.fn, T_OP["set_interval"], body)
        assert (app.interval, app.average) == (500, 4)
    else:
        with pytest.raises(h.Unsupported) as e:
            hst.call(t.fn, T_OP["set_interval"], body)
        assert e.value.tag == NAMED_EXTENSION | m.TAG_CRITICAL and app.interval == 1000
    hst.call(t.fn, T_OP["set_interval"], struct.pack("<I", 700) + named(b"com.b.hint", b"x"))
    assert app.interval == 700


def test_another_party_adds_a_choice_to_a_named_list():
    """core §7.7: B's firmware lists its own sensor first, so A's sensors move. A host that looks numbers up by name
    reads the same sensors from both firmwares; one that kept A's numbers reads B's sensor as 'board'."""
    plain, _ = connect()
    tip, _ = connect("b_tip")
    for hst in (plain, tip):
        t = ThermoHost(hst)
        assert t.read()["ambient"] == 22.75 and t.read()["board"] == 31.5
    t = ThermoHost(tip)
    assert t.sensors == [B_TIP, "board", "ambient"] and t.read()[B_TIP] == 85.125
    p = t.read_raw()
    kept_board = 1                                         # A's number for board on A's own firmware
    assert struct.unpack_from("<i", p, 2 + 5 * (kept_board - 1))[0] / 1000 != 31.5


def test_a_new_operation_goes_into_the_other_partys_own_interface():
    """core §12 rule 7: B needs an operation A does not have (calibrate). It is B's interface on its own fn; its request
    names A's fn and A's sensor number, both found by name this boot. A's host does not see it; the effect shows in A's
    read."""
    hst, app = connect("calib")
    assert [e.name for e in core.list_entries(hst)] == [THERMO, CALIB]
    t = ThermoHost(hst)
    calib = core.find(hst, CALIB, 1)
    r = hst.call(calib, C_OP["calibrate"], struct.pack("<HBi", t.fn, t.number("ambient"), -250))
    assert struct.unpack("<i", r.payload[:4])[0] == -250
    assert t.read()["ambient"] == 22.5
    with pytest.raises(h.Rejected) as e:                   # the same op number on A's fn is A's read (another form)
        hst.call(t.fn, C_OP["calibrate"], struct.pack("<HBi", t.fn, 1, 0))
    assert e.value.result.detail == R["malformed"] and app.offset == {"ambient": -250}


def test_a_third_party_extends_the_second_partys_interface():
    """C extends B's interface as B extended A's: a named extension in calibrate's answer. B's host takes the offset as
    always; C's reads the certificate."""
    hst, app = connect("calib", "c_cert")
    t = ThermoHost(hst)
    r = hst.call(core.find(hst, CALIB, 1), C_OP["calibrate"], struct.pack("<HBi", t.fn, t.number("board"), 100))
    assert struct.unpack_from("<i", r.payload)[0] == 100
    assert extensions(r.payload[4:]) == {C_CERT: b"CAL-2026-0042"}


def test_a_changed_fixed_form_is_a_new_revision_beside_the_old():
    """core §2.8 / §7.2: A widens the reading (i64 µ°C) - a fixed form changed, so revision 2. A firmware offering both
    keeps the first release's hosts working; one offering only 2 is not used by them (never misread)."""
    hst, app = connect("rev2")
    assert ThermoHost(hst).read() == {"board": 31.5, "ambient": 22.75}
    fn2 = core.find(hst, THERMO, 2)
    p = hst.call(fn2, T_OP["read"], b"", locked=False).payload
    assert struct.unpack_from("<q", p, 2)[0] == 31_500_000
    only2, _ = connect("only_rev2")
    with pytest.raises(core.UnsupportedRevision):
        ThermoHost(only2)


def test_a_number_another_party_took_collides_a_name_does_not():
    """Why core §12 rule 7 lets others add only by names: B (against the rule) put humidity in A's answer TLV 0x02;
    A later defines 0x02 as battery_mv (a legal addition, §12.1). B's host on A's new firmware reads the battery as
    humidity - silently wrong. B's named humidity on the same firmware is read right, and absent where it is absent."""
    raw_reader = lambda tail: struct.unpack_from("<BH", dict(m.split_tlvs(tail))[T_READ_ANSWER["battery_mv"]])[1] / 10
    b_firmware, _ = connect("b_raw_humidity")
    b = BHost(b_firmware)
    b.read()
    assert raw_reader(b.tail) == 45.1                      # on B's own firmware it "works"
    a_firmware, _ = connect("battery")
    b = BHost(a_firmware)
    b.read()
    with pytest.raises(struct.error):                      # A's 2-byte battery is no (sensor, permille): or worse,
        raw_reader(b.tail)                                 # a same-sized value read as humidity
    assert BHost(a_firmware).humidity() == {}              # by name: none there, nothing misread
    named_firmware, _ = connect("battery", "b_humidity")
    assert BHost(named_firmware).humidity() == {"ambient": 45.1}


def test_an_extension_that_changes_a_fixed_meaning_is_unsafe_as_a_named_extension():
    """core §12 rule 7's last line: B wants read in °F. As a non-critical named extension, a firmware without it
    answers °C and B's host reads them as °F - silently wrong; even critical, every reading's meaning would depend on
    a TLV a generic reader (a log, a dump) does not see. A changed meaning of a fixed field is B's own interface (or A's
    next revision), never an extension of A's."""
    b_firmware, _ = connect("b_fahrenheit")
    a_firmware, _ = connect()
    fahrenheit = named(b"com.b.fahrenheit", b"")
    assert ThermoHost(b_firmware).read(fahrenheit)["ambient"] == 72.95
    assert ThermoHost(a_firmware).read(fahrenheit)["ambient"] == 22.75   # B's host would take 22.75 as °F
    assert ThermoHost(b_firmware).read()["ambient"] == 22.75             # and a generic reader of B's traffic, too
