"""The v1 client API in one place, for callers written against the first draft (oep_smoke, the experiments).

New code can import the modules directly: core (finding interfaces, the plan), riscv (oep.wire.rvswd / swio,
oep.target.riscv-dm), console (oep.target.console), fixture (gpio / uart), capture (oep.fixture.capture), arm
(oep.wire.swd, oep.target.arm-adi).
"""

from .capture import LogicCapture  # noqa: F401
from .console import MARK_NAMES, Console, ConsoleIO, Mark, StreamIO  # noqa: F401
from .core import (OP_PLAN_APPLY, OP_PLAN_RELEASE, Interface, UnsupportedRevision, confirm, find,  # noqa: F401
                   find_all, plan_apply, plan_release, probe_labels)
from .fixture import FixtureUart, FixtureUartIO, Gpio  # noqa: F401
from .host import Failed, NoConnection, NotV1, OepError, Rejected, Unsupported  # noqa: F401
from .riscv import (Found, RiscvDm, RunResult, StepListError, TargetError, Wire, WireBase,  # noqa: F401
                    attach_after_gpio_reset, ran)
from . import host as h  # noqa: F401  (callers use target.h.Rejected)
