"""host guide §5.3: an OEP address, oep://<probe>[/<port>[/<target>]][?via=<route>]."""
import pytest

from oep_client import link


@pytest.mark.parametrize("text, want", [
    ("oep://30eda0e31108", link.Address("30eda0e31108")),
    ("OEP://30EDA0E31108/x035/v003", link.Address("30EDA0E31108", "x035", "v003")),
    ("oep://30eda0e31108/x035?via=bulk", link.Address("30eda0e31108", "x035", None, "bulk")),
    ("oep://30eda0e31108/x035?via=tty:/dev/serial/by-id/usb-x-if00", link.Address("30eda0e31108", "x035", None,
                                                                              "tty:/dev/serial/by-id/usb-x-if00")),
    ("oep://30eda0e31108?via=tcp:192.168.1.5:7450", link.Address("30eda0e31108", via="tcp:192.168.1.5:7450")),
    ("oep://30eda0e31108?via=tty:COM3%3F", link.Address("30eda0e31108", via="tty:COM3?")),
])
def test_an_address_parts_its_place_after_its_query(text, want):
    """The query comes off first: a tty route's path has '/', yet the place is still <probe>[/<port>[/<target>]]."""
    assert link.parse_address(text) == want


def test_the_place_leaves_the_route_out():
    """Two addresses name the same place, and device locks share a key, whatever the route."""
    a = link.parse_address("oep://30eda0e31108/x035?via=net")
    b = link.parse_address("oep://30eda0e31108/x035?via=tty:/dev/ttyACM0")
    assert a.place == b.place == "oep://30eda0e31108/x035"


@pytest.mark.parametrize("text", [
    "oep://30eda0e31108?port=x035",                  # an unknown query
    "oep://30eda0e31108?via=net&via=usb",            # one via only
    "oep://30eda0e31108?via=serial:/dev/ttyACM0",    # not a route
    "oep://30eda0e31108?via=tty:",                   # a route without its value
    "oep://30eda0e31108/a/b/c",                      # four segments
    "oep://30eda0e31108/x:035",                      # ':' is not in a segment
    "oep://x-esp32",                                 # an x- unit_id names no unit (core §7.5)
    "tcp://127.0.0.1:7450",
])
def test_an_address_out_of_its_form_is_refused(text):
    with pytest.raises(ValueError):
        link.parse_address(text)
