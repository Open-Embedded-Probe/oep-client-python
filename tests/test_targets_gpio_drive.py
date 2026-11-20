"""targets.gpio_drive: the register and value that set or clear a CH32 pad, the CH32X035's pads 16-23 included."""
from oep_client import targets


def test_low_pads_use_bshr_halves():
    assert targets.gpio_drive(3, True) == (targets.GPIO_BSHR, 1 << 3)
    assert targets.gpio_drive(15, False) == (targets.GPIO_BSHR, 1 << 31)


def test_x035_high_pads_use_bsxr_and_bcr():
    assert targets.gpio_drive(16, True) == (targets.GPIO_BSXR, 1 << 0)
    assert targets.gpio_drive(23, True) == (targets.GPIO_BSXR, 1 << 7)
    assert targets.gpio_drive(21, False) == (targets.GPIO_BCR, 1 << 21)
    for bit in range(24):
        for high in (True, False):
            assert targets.gpio_drive(bit, high)[1] <= 0xFFFFFFFF
