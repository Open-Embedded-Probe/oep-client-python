import argparse
import hashlib
from pathlib import Path

from .prototype import (
    Endpoint, FunctionResult, SerialConnection, TargetControlClient,
    TargetFlashClient,
    TargetMemoryClient,
)
from .flash_image import padded_image, program_image, read_range, reset_target, verify_image


def progress(operation: str, completed: int, total: int) -> None:
    interval = 128 if operation == "program" else 4096
    if completed == total or completed % interval == 0:
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
                        help="read 4..32 aligned bytes; integers accept 0x prefix")
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
    args = parser.parse_args()
    endpoint = Endpoint()
    connection = SerialConnection(args.port, timeout=args.timeout)
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
            address, length = (int(value, 0) for value in args.read_memory)
            result = TargetMemoryClient(endpoint, connection).read(address, length)
            if isinstance(result, FunctionResult):
                print_result("read-memory", result)
            else:
                print(f"read-memory address=0x{address:08x} data={result.hex()}")
        if args.program_page64:
            address = int(args.program_page64[0], 0)
            data = bytes.fromhex(args.program_page64[1])
            result = TargetFlashClient(endpoint, connection).program_page64(address, data)
            print_result("program-page64", result)
        memory = TargetMemoryClient(endpoint, connection)
        if args.backup_flash:
            data = read_range(memory, args.flash_base, args.flash_size, progress)
            Path(args.backup_flash).write_bytes(data)
            reset_target(TargetControlClient(endpoint, connection))
            print(f"backup-flash bytes={len(data)} sha256={hashlib.sha256(data).hexdigest()}")
        if args.program_image:
            if not args.destructive:
                parser.error("--program-image requires --destructive")
            desired = padded_image(Path(args.program_image).read_bytes(), args.flash_size)
            summary = program_image(
                memory, TargetFlashClient(endpoint, connection),
                args.flash_base, desired, progress=progress)
            reset_target(TargetControlClient(endpoint, connection))
            verify_image(memory, args.flash_base, desired, progress)
            reset_target(TargetControlClient(endpoint, connection))
            print(f"program-image pages={summary.pages_programmed} "
                  f"attempts={summary.attempts} sha256={hashlib.sha256(desired).hexdigest()}")
        if args.verify_image:
            expected = padded_image(Path(args.verify_image).read_bytes(), args.flash_size)
            verify_image(memory, args.flash_base, expected, progress)
            reset_target(TargetControlClient(endpoint, connection))
            print(f"verify-image bytes={len(expected)} sha256={hashlib.sha256(expected).hexdigest()}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
