#!/usr/bin/env python3
"""Destructive-prototype smoke test for the UIAPduino UART/GPIO fixture."""

import argparse
import time

from oep_client import Endpoint, FixtureGpioClient, FixtureUartClient, SerialConnection


def receive_line(uart: FixtureUartClient, timeout: float = 3.0) -> bytes:
    received = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        part = uart.read_available(80)
        if not isinstance(part, bytes):
            raise RuntimeError(f"UART read failed: {part}")
        received += part
        if b"\n" in received:
            return bytes(received)
        time.sleep(0.03)
    raise TimeoutError(bytes(received))


def command(uart: FixtureUartClient, command_bytes: bytes) -> bytes:
    written = uart.write(command_bytes)
    if written != len(command_bytes):
        raise RuntimeError(f"UART write failed: {written}")
    return receive_line(uart)


def main(port: str) -> None:
    connection = SerialConnection(port, timeout=8)
    endpoint = Endpoint()
    try:
        uart = FixtureUartClient(endpoint, connection)
        gpio = FixtureGpioClient(endpoint, connection)
        if uart.configure(115200) != 115200:
            raise RuntimeError("UART configuration failed")
        if b"PONG" not in command(uart, b"PING\n"):
            raise RuntimeError("fixture did not answer PING")

        for target_pin, esp_gpio in ((7, 27), (9, 14)):
            for value in (0, 1, 0):
                expected = f"DOUT pin={target_pin} value={value}".encode()
                response = command(
                    uart, f"DOUT {target_pin} {value}\n".encode())
                if expected not in response:
                    raise RuntimeError(response)
                time.sleep(0.1)
                actual = gpio.read_digital(esp_gpio)
                if actual != value:
                    raise RuntimeError(
                        f"DOUT {target_pin}={value}, GPIO{esp_gpio}={actual}")
                print(f"PASS target_pin={target_pin} gpio={esp_gpio} value={value}")
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    main(parser.parse_args().port)
