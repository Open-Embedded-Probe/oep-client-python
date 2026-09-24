"""The v1 client API in one place, for callers written against the first draft (oep_smoke, the experiments).

New code can import the modules directly: core (finding interfaces, the plan), riscv (oep.wire.rvswd / swio,
oep.target.riscv-dm), console (oep.target.console), fixture (gpio / uart / capture), arm (oep.wire.swd,
oep.target.arm-adi).
"""

from .console import MARK_NAMES, Console, ConsoleIO, Mark  # noqa: F401
from .core import (OP_PLAN_APPLY, OP_PLAN_RELEASE, Interface, confirm, find, find_all,  # noqa: F401
                   plan_apply, plan_release, probe_labels)
from .fixture import Capture, FixtureUartIO, Gpio  # noqa: F401
from .host import Failed, OepError, Rejected  # noqa: F401
from .riscv import Found, RiscvDm, StepListError, Wire, WireBase, attach_after_gpio_reset  # noqa: F401
from . import host as h  # noqa: F401  (callers use target.h.Rejected)
