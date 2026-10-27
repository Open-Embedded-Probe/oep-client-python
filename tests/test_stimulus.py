"""oep_client.stimulus: the judge of whether an ADC channel follows a driven line, and the level a crossing passes."""

from oep_client import stimulus


def test_judge_needs_the_looks_apart_by_half_the_range():
    assert stimulus.judge([0, 1, 3], [3197, 3227, 3233], 4095)          # a target's UART line, its power off and on
    assert not stimulus.judge([0, 383, 112], [0, 604, 122], 4095)       # a floating input: noise either way
    assert not stimulus.judge([0, 10], [1500, 1600], 4095)              # apart, but not by half the range
    assert not stimulus.judge([0, 2100], [2050, 4095], 4095)            # overlapping looks
    assert not stimulus.judge([], [4095], 4095)


def test_level_is_between_the_ends_seen():
    s = stimulus.Stimulus(drive=5, analog=22, rest_high=True, low=1, high=3227, low_max=3, high_min=3197)
    assert s.level() == 1600 and s.low_max < s.level() < s.high_min
