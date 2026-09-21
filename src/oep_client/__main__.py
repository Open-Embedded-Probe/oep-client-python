import argparse
import hashlib
import json
from pathlib import Path
import time

from .prototype import (
    ConnectionBusyError, Endpoint, FixtureGpioClient, FunctionResult, SerialConnection, TargetControlClient,
    TargetFlashClient,
    TargetMemoryClient,
)
from .flash_image import padded_image, program_image, read_range, reset_target, verify_image


def progress(operation: str, completed: int, total: int) -> None:
    # Updates are measured in pages for program and in transport-sized byte
    # chunks for reads. Report each 1/16 boundary instead of using a byte
    # modulus, which went silent when the read chunk changed from 32 to 88.
    previous = completed - (1 if operation == "program" else min(88, completed))
    if completed == total or completed * 16 // total != previous * 16 // total:
        print(f"{operation} {completed}/{total}", flush=True)


def print_result(name: str, result: FunctionResult) -> None:
    suffix = f" data={result.data.hex()}" if result.data else ""
    print(f"{name} resolution={result.resolution} detail={result.detail}{suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Destructive OEP P0 prototype")
    parser.add_argument("--port", required=True)
    parser.add_argument("--timeout", type=float, default=45.0,
                        help="response timeout in seconds; default: 45")
    parser.add_argument("--target", choices=("status", "normalize-user", "bootloader"))
    parser.add_argument("--read-memory", nargs=2, metavar=("ADDRESS", "LENGTH"),
                        help="read 4..88 aligned bytes; integers accept 0x prefix")
    parser.add_argument("--gpio-read", metavar="PIN", type=lambda value: int(value, 0),
                        help="read one probe-side FixtureGpio pin")
    parser.add_argument("--program-page64", nargs=2, metavar=("ADDRESS", "HEX"),
                        help="destructively program exactly 64 bytes")
    parser.add_argument("--backup-flash", metavar="FILE",
                        help="read the configured flash range into FILE")
    parser.add_argument("--program-image", metavar="FILE",
                        help="diff, program, reset, and verify a raw binary image")
    parser.add_argument("--verify-image", metavar="FILE",
                        help="verify a padded raw binary image without programming")
    parser.add_argument("--flash-base", type=lambda value: int(value, 0),
                        default=0x08000000)
    parser.add_argument("--flash-size", type=lambda value: int(value, 0),
                        default=63488,
                        help="default: 63488 bytes (CH32X035)")
    parser.add_argument("--destructive", action="store_true",
                        help="required with --program-image")
    parser.add_argument("--result-json", metavar="FILE",
                        help="write machine-readable operation timings on completion")
    args = parser.parse_args()
    endpoint = Endpoint()
    try:
        connection = SerialConnection(args.port, timeout=args.timeout)
    except ConnectionBusyError as error:
        parser.error(str(error))
    cleanup_target: TargetControlClient | None = None
    timings: dict[str, float] = {}

    def timed(name, operation):
        started = time.monotonic()
        try:
            return operation()
        finally:
            timings[name] = round(time.monotonic() - started, 6)

    try:
        correlation, request = endpoint.confirm_request()
        confirmation = endpoint.parse_confirm(connection.exchange(request), correlation)
        print(f"endpoint revision={confirmation['revision']} max_message={confirmation['maximum_message']}")
        correlation, request = endpoint.list_functions_request()
        functions = endpoint.parse_functions(connection.exchange(request), correlation)
        for function in functions:
            print(f"function reference=0x{function.reference:04x} revision={function.revision} flags=0x{function.flags:02x}")
        if args.target:
            target = TargetControlClient(endpoint, connection)
            # Entering the product bootloader is an explicit terminal state;
            # every diagnostic operation must instead leave the target running.
            if args.target != "bootloader":
                cleanup_target = target
            if args.target == "status":
                result = target.get_status()
                if isinstance(result, FunctionResult):
                    print_result("target-status", result)
                else:
                    print("target-status " + " ".join(
                        f"{key}={value}" for key, value in result.items()))
            elif args.target == "normalize-user":
                print_result("normalize-user", target.normalize_user())
            else:
                print_result("bootloader", target.enter_product_bootloader())
        if args.read_memory:
            cleanup_target = TargetControlClient(endpoint, connection)
            address, length = (int(value, 0) for value in args.read_memory)
            result = TargetMemoryClient(endpoint, connection).read(address, length)
            if isinstance(result, FunctionResult):
                print_result("read-memory", result)
            else:
                print(f"read-memory address=0x{address:08x} data={result.hex()}")
        if args.gpio_read is not None:
            result = FixtureGpioClient(endpoint, connection).read_digital(args.gpio_read)
            if isinstance(result, FunctionResult):
                print_result("gpio-read", result)
            else:
                print(f"gpio-read pin={args.gpio_read} value={result}")
        if args.program_page64:
            cleanup_target = TargetControlClient(endpoint, connection)
            address = int(args.program_page64[0], 0)
            data = bytes.fromhex(args.program_page64[1])
            result = TargetFlashClient(endpoint, connection).program_page64(address, data)
            print_result("program-page64", result)
        memory = TargetMemoryClient(endpoint, connection)
        if args.backup_flash:
            cleanup_target = TargetControlClient(endpoint, connection)
            data = timed("backup_read", lambda: read_range(
                memory, args.flash_base, args.flash_size, progress))
            Path(args.backup_flash).write_bytes(data)
            timed("backup_reset", lambda: reset_target(cleanup_target))
            print(f"backup-flash bytes={len(data)} sha256={hashlib.sha256(data).hexdigest()}")
        if args.program_image:
            if not args.destructive:
                parser.error("--program-image requires --destructive")
            desired = padded_image(Path(args.program_image).read_bytes(), args.flash_size)
            cleanup_target = TargetControlClient(endpoint, connection)
            summary = timed("program", lambda: program_image(
                memory, TargetFlashClient(endpoint, connection),
                args.flash_base, desired, progress=progress))
            timed("program_reset", lambda: reset_target(cleanup_target))
            timed("program_verify", lambda: verify_image(
                memory, args.flash_base, desired, progress))
            timed("program_verify_reset", lambda: reset_target(cleanup_target))
            print(f"program-image pages={summary.pages_programmed} "
                  f"attempts={summary.attempts} sha256={hashlib.sha256(desired).hexdigest()}")
        if args.verify_image:
            expected = padded_image(Path(args.verify_image).read_bytes(), args.flash_size)
            cleanup_target = TargetControlClient(endpoint, connection)
            timed("verify", lambda: verify_image(
                memory, args.flash_base, expected, progress))
            timed("verify_reset", lambda: reset_target(cleanup_target))
            print(f"verify-image bytes={len(expected)} sha256={hashlib.sha256(expected).hexdigest()}")
        if args.result_json:
            result = {
                "port": args.port,
                "flash_base": args.flash_base,
                "flash_size": args.flash_size,
                "timings_seconds": timings,
            }
            if args.program_image:
                result["program"] = {
                    "bytes_compared": summary.bytes_compared,
                    "pages_programmed": summary.pages_programmed,
                    "attempts": summary.attempts,
                }
            Path(args.result_json).write_text(
                json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    finally:
        # Every target memory/flash operation halts through RVSWD. This also
        # covers an exception between an operation and its normal reset.
        if cleanup_target is not None:
            try:
                reset_target(cleanup_target)
            except Exception:
                pass
        connection.close()


if __name__ == "__main__":
    main()
