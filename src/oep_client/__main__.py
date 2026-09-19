import argparse

from .prototype import Endpoint, SerialConnection


def main() -> None:
    parser = argparse.ArgumentParser(description="Destructive OEP P0 prototype")
    parser.add_argument("--port", required=True)
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
    finally:
        connection.close()


if __name__ == "__main__":
    main()
