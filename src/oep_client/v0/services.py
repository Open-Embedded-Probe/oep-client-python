"""Typed wrappers for the v0 standard definitions. Each takes the offered-function reference."""

from __future__ import annotations

from . import codec
from .client import Client, RequestError


class ProbeIdentity:
    DEFINITION = codec.DEF_PROBE_IDENTITY

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def get(self) -> codec.ProbeIdentityGetResult:
        response = self.client.call(self.function, codec.PROBE_IDENTITY_OP_GET, codec.ProbeIdentityGetRequest().pack())
        return codec.ProbeIdentityGetResult.unpack(response.expect_success("probe.identity get"))


def _crc32(data: bytes) -> int:
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


class TargetControl:
    DEFINITION = codec.DEF_TARGET_CONTROL

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def status(self) -> codec.TargetControlStatusResult:
        response = self.client.call(self.function, codec.TARGET_CONTROL_OP_STATUS)
        return codec.TargetControlStatusResult.unpack(response.expect_success("target.control status"))

    def halt(self) -> None:
        self.client.call(self.function, codec.TARGET_CONTROL_OP_HALT).expect_success("target.control halt")

    def resume(self) -> None:
        self.client.call(self.function, codec.TARGET_CONTROL_OP_RESUME).expect_success("target.control resume")

    def reset(self, mode: int = 0) -> None:
        self.client.call(self.function, codec.TARGET_CONTROL_OP_RESET,
                         codec.TargetControlResetRequest(mode=mode).pack()).expect_success("target.control reset")

    def read_dmi(self, address: int) -> int:
        response = self.client.call(self.function, codec.TARGET_CONTROL_OP_READ_DMI,
                                    codec.TargetControlReadDmiRequest(address=address).pack())
        return codec.TargetControlReadDmiResult.unpack(response.expect_success("target.control read_dmi")).value

    def read_register(self, regno: int) -> int:
        """Abstract-command register read while halted: CSR number, or 0x1000 + GPR index."""
        response = self.client.call(self.function, codec.TARGET_CONTROL_OP_READ_REGISTER,
                                    codec.TargetControlReadRegisterRequest(regno=regno).pack())
        return codec.TargetControlReadRegisterResult.unpack(response.expect_success("target.control read_register")).value


class TargetMemory:
    DEFINITION = codec.DEF_TARGET_MEMORY

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    @property
    def chunk(self) -> int:
        # result = 5-byte header + data; keep it 4-aligned and within max_frame
        return (self.client.limits.max_frame - 5) & ~3

    def read(self, address: int, length: int) -> bytes:
        response = self.client.call(self.function, codec.TARGET_MEMORY_OP_READ,
                                    codec.TargetMemoryReadRequest(address=address, length=length).pack())
        return codec.TargetMemoryReadResult.unpack(response.expect_success("target.memory read")).data

    def read_word(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "little")

    def read_range(self, address: int, length: int) -> bytes:
        """Pipelined read honoring the probe's window; results arrive in order."""
        chunk = self.chunk
        requests = [(self.function, codec.TARGET_MEMORY_OP_READ,
                     codec.TargetMemoryReadRequest(address=address + off, length=min(chunk, length - off)).pack())
                    for off in range(0, length, chunk)]
        out = bytearray()
        for response in self.client.pipeline(requests):
            out += codec.TargetMemoryReadResult.unpack(response.expect_success("target.memory read")).data
        return bytes(out)

    def write(self, address: int, data: bytes) -> int:
        response = self.client.call(self.function, codec.TARGET_MEMORY_OP_WRITE,
                                    codec.TargetMemoryWriteRequest(address=address, data=data).pack())
        return codec.TargetMemoryWriteResult.unpack(response.expect_success("target.memory write")).written


class TargetFlash:
    DEFINITION = codec.DEF_TARGET_FLASH

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def geometry(self) -> codec.TargetFlashGeometryResult:
        response = self.client.call(self.function, codec.TARGET_FLASH_OP_GEOMETRY)
        return codec.TargetFlashGeometryResult.unpack(response.expect_success("target.flash geometry"))

    def erase_page(self, address: int) -> None:
        self.client.call(self.function, codec.TARGET_FLASH_OP_ERASE_PAGE,
                         codec.TargetFlashErasePageRequest(address=address).pack()).expect_success("target.flash erase")

    def program_page(self, address: int, data: bytes) -> None:
        self.client.call(self.function, codec.TARGET_FLASH_OP_PROGRAM_PAGE,
                         codec.TargetFlashProgramPageRequest(address=address, data=data).pack()).expect_success("target.flash program")

    def program_pages(self, pages: list[tuple[int, bytes]]) -> list[int]:
        """Pipelined page programs. Returns the addresses whose result was not success."""
        requests = [(self.function, codec.TARGET_FLASH_OP_PROGRAM_PAGE,
                     codec.TargetFlashProgramPageRequest(address=a, data=d).pack()) for a, d in pages]
        failed = []
        for (address, _), response in zip(pages, self.client.pipeline(requests)):
            if not response.succeeded:
                failed.append(address)
        return failed

    def verify_crc32(self, address: int, length: int) -> int:
        response = self.client.call(self.function, codec.TARGET_FLASH_OP_VERIFY_CRC32,
                                    codec.TargetFlashVerifyCrc32Request(address=address, length=length).pack())
        return codec.TargetFlashVerifyCrc32Result.unpack(response.expect_success("target.flash verify")).crc32

    @staticmethod
    def crc32(data: bytes) -> int:
        return _crc32(data)


class FixtureGpio:
    DEFINITION = codec.DEF_FIXTURE_GPIO
    INPUT_FLOATING, INPUT_PULL_UP, INPUT_PULL_DOWN, INPUT_PULL_UP_DOWN = 0, 1, 2, 3
    OUTPUT_LOW, OUTPUT_HIGH, OPEN_DRAIN_LOW, OPEN_DRAIN_RELEASE = 4, 5, 6, 7

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def configure(self, channel: int, mode: int) -> None:
        self.client.call(self.function, codec.FIXTURE_GPIO_OP_CONFIGURE,
                         codec.FixtureGpioConfigureRequest(channel=channel, mode=mode).pack()).expect_success("fixture.gpio configure")

    def read_bank(self) -> tuple[int, int]:
        response = self.client.call(self.function, codec.FIXTURE_GPIO_OP_READ_BANK)
        result = codec.FixtureGpioReadBankResult.unpack(response.expect_success("fixture.gpio read_bank"))
        return result.available, result.values

    def read(self, channel: int) -> int:
        available, values = self.read_bank()
        if not (available >> channel) & 1:
            raise RequestError(f"channel {channel} is not a fixture GPIO")
        return (values >> channel) & 1


class FixtureUart:
    DEFINITION = codec.DEF_FIXTURE_UART
    ROLE_RX, ROLE_TX = 1, 2

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def assignments(self, rx: int, tx: int):
        return [(self.function, self.ROLE_RX, rx), (self.function, self.ROLE_TX, tx)]

    def configure(self, baud: int) -> int:
        response = self.client.call(self.function, codec.FIXTURE_UART_OP_CONFIGURE, codec.FixtureUartConfigureRequest(baud=baud).pack())
        return codec.FixtureUartConfigureResult.unpack(response.expect_success("fixture.uart configure")).actual_baud

    def write(self, data: bytes) -> int:
        response = self.client.call(self.function, codec.FIXTURE_UART_OP_WRITE, codec.FixtureUartWriteRequest(data=data).pack())
        return codec.FixtureUartWriteResult.unpack(response.expect_success("fixture.uart write")).written

    def read(self, maximum: int = 512) -> bytes:
        response = self.client.call(self.function, codec.FIXTURE_UART_OP_READ, codec.FixtureUartReadRequest(maximum=maximum).pack())
        return codec.FixtureUartReadResult.unpack(response.expect_success("fixture.uart read")).data

    def read_until(self, terminator: bytes, timeout: float = 2.0) -> bytes:
        import time
        deadline, buf = time.monotonic() + timeout, bytearray()
        while time.monotonic() < deadline:
            buf += self.read()
            if terminator in buf:
                return bytes(buf)
            time.sleep(0.005)
        return bytes(buf)


class P4I2cTarget:
    """Vendor tool (owner 0x0100): ESP32-P4 hardware I2C target with the E147-E150 slot/framing contract."""
    DEFINITION = codec.DEF_P4_I2C_TARGET
    MODE_FIXED_RX, MODE_FRAMED_RX, MODE_PRELOADED_TX = 1, 2, 3
    ROLE_SDA, ROLE_SCL = 1, 2

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def assignments(self, sda: int, scl: int):
        return [(self.function, self.ROLE_SDA, sda), (self.function, self.ROLE_SCL, scl)]

    def configure(self, address: int, mode: int) -> None:
        self.client.call(self.function, codec.P4_I2C_TARGET_OP_CONFIGURE,
                         codec.P4I2cTargetConfigureRequest(address=address, mode=mode).pack()).expect_success("p4.i2c-target configure")

    def arm_rx(self, length: int) -> None:
        self.client.call(self.function, codec.P4_I2C_TARGET_OP_ARM_RX,
                         codec.P4I2cTargetArmRxRequest(length=length).pack()).expect_success("p4.i2c-target arm_rx")

    def read_rx(self) -> tuple[int, bytes]:
        response = self.client.call(self.function, codec.P4_I2C_TARGET_OP_READ_RX)
        result = codec.P4I2cTargetReadRxResult.unpack(response.expect_success("p4.i2c-target read_rx"))
        return result.pending, result.data

    def preload_tx(self, data: bytes) -> int:
        response = self.client.call(self.function, codec.P4_I2C_TARGET_OP_PRELOAD_TX,
                                    codec.P4I2cTargetPreloadTxRequest(data=data).pack())
        return codec.P4I2cTargetPreloadTxResult.unpack(response.expect_success("p4.i2c-target preload_tx")).slots

    def status(self) -> codec.P4I2cTargetStatusResult:
        response = self.client.call(self.function, codec.P4_I2C_TARGET_OP_STATUS)
        return codec.P4I2cTargetStatusResult.unpack(response.expect_success("p4.i2c-target status"))

    def reset(self) -> None:
        self.client.call(self.function, codec.P4_I2C_TARGET_OP_RESET).expect_success("p4.i2c-target reset")
