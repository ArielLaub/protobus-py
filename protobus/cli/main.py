"""Main CLI entry point for Protobus tools."""

import argparse
import sys
from typing import Optional

from .generate_types import generate_types
from .generate_service import generate_service


def main(args: Optional[list[str]] = None) -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="protobus",
        description="Protobus CLI tools for type generation and service scaffolding",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Generate types command
    types_parser = subparsers.add_parser(
        "generate",
        help="Generate Python types from .proto files",
    )
    types_parser.add_argument(
        "--proto-dir",
        help="Directory containing .proto files",
    )
    types_parser.add_argument(
        "--output",
        "-o",
        help="Output file path",
    )

    # Generate service command
    service_parser = subparsers.add_parser(
        "generate:service",
        help="Generate a service stub class",
    )
    service_parser.add_argument(
        "service_name",
        help="Full service name (e.g., calculator.MathService)",
    )
    service_parser.add_argument(
        "--output-dir",
        "-o",
        help="Output directory for generated service",
    )

    # Init command
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a new Protobus project",
    )

    parsed = parser.parse_args(args)

    if parsed.command == "generate":
        success = generate_types(
            proto_dir=parsed.proto_dir,
            output=parsed.output,
        )
        return 0 if success else 1

    elif parsed.command == "generate:service":
        success = generate_service(
            parsed.service_name,
            output_dir=parsed.output_dir,
        )
        return 0 if success else 1

    elif parsed.command == "init":
        print("Protobus Project Setup")
        print("=" * 40)
        print()
        print("1. Add the following to your pyproject.toml:")
        print()
        print("   [tool.protobus]")
        print('   protoDir = "./proto"')
        print('   typesOutput = "./types/proto.py"')
        print('   servicesDir = "./services"')
        print()
        print("2. Create your proto directory:")
        print("   mkdir -p proto")
        print()
        print("3. Create your .proto files in the proto directory")
        print()
        print("4. Generate types:")
        print("   protobus generate")
        print()
        print("5. Generate service stubs:")
        print("   protobus generate:service package.ServiceName")
        print()
        return 0

    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
