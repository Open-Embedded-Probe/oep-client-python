import argparse

from .prototype import (
    Endpoint, FunctionResult, SerialConnection, TargetControlClient,
    TargetFlashClient,
    TargetMemoryClient,
)


def print_result(name: str, result: FunctionResult) -> None:
    print(f"{name} resolution={result.resolution} detail={result.detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Destructive OEP P0 prototype")
    parser.add_argument("--port", required=True)
    parser.add_argument("--target", choices=("status", "normalize-user", "bootloader"))
    parser.add_argument("--read-memory", nargs=2, metavar=("ADDRESS", "LENGTH"),
                        help="read 4..32 aligned bytes; integers accept 0x prefix")
    parser.add_argument("--program-page64", nargs=2, metavar=("ADDRESS", "HEX"),
                        help="destructively program exactly 64 bytes")
    args = parser.parse_args()
    endpoint = Endpoint()
    connection = SerialConnection(args.port)
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
    finally:
        connection.close()


if __name__ == "__main__":
    main()
