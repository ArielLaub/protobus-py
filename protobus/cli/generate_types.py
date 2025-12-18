"""Generate Python type definitions from .proto files."""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import CliConfig, find_proto_files, load_config


def generate_types(
    config: Optional[CliConfig] = None,
    proto_dir: Optional[str] = None,
    output: Optional[str] = None,
) -> bool:
    """
    Generate Python type definitions from Protocol Buffer files.

    This command:
    1. Finds all .proto files in the proto directory
    2. Uses protoc to generate Python code
    3. Creates type stubs with service name constants

    Args:
        config: CLI configuration (loaded from pyproject.toml if not provided)
        proto_dir: Override proto directory from config
        output: Override output path from config

    Returns:
        True if generation succeeded, False otherwise
    """
    cfg = config or load_config()
    proto_directory = proto_dir or cfg.proto_dir
    output_path = output or cfg.types_output

    print(f"Generating types from {proto_directory}...")

    # Find all proto files
    proto_files = find_proto_files(proto_directory)

    if not proto_files:
        print(f"No .proto files found in {proto_directory}")
        return False

    print(f"Found {len(proto_files)} proto file(s)")

    # Ensure output directory exists
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate Python code using protoc
    try:
        for proto_file in proto_files:
            result = subprocess.run(
                [
                    "protoc",
                    f"--proto_path={proto_directory}",
                    f"--python_out={output_dir}",
                    f"--pyi_out={output_dir}",
                    proto_file,
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                print(f"Error generating types for {proto_file}:")
                print(result.stderr)
                return False

            print(f"  Generated types for {proto_file}")

    except FileNotFoundError:
        print("Error: protoc not found. Please install Protocol Buffers compiler.")
        print("  macOS: brew install protobuf")
        print("  Ubuntu: apt install protobuf-compiler")
        print("  Windows: choco install protoc")
        return False

    # Generate service name constants
    _generate_service_constants(proto_files, output_path, proto_directory)

    print(f"Types generated successfully to {output_path}")
    return True


def _generate_service_constants(
    proto_files: list[str],
    output_path: str,
    proto_dir: str,
) -> None:
    """Generate a Python file with service name constants."""
    services: list[tuple[str, str]] = []  # (service_name, proto_file)

    for proto_file in proto_files:
        with open(proto_file, "r") as f:
            content = f.read()

        # Extract package name
        package_match = re.search(r'package\s+([a-zA-Z0-9_.]+)\s*;', content)
        package = package_match.group(1) if package_match else ""

        # Extract service names
        service_matches = re.findall(r'service\s+([a-zA-Z0-9_]+)\s*\{', content)

        for service_name in service_matches:
            full_name = f"{package}.{service_name}" if package else service_name
            proto_name = Path(proto_file).relative_to(proto_dir)
            services.append((full_name, str(proto_name)))

    if services:
        constants_file = Path(output_path)

        with open(constants_file, "w") as f:
            f.write('"""Auto-generated service name constants."""\n\n')
            f.write("# Service name constants\n")
            f.write("# Use these constants instead of hardcoding service names\n\n")

            for service_name, proto_file in services:
                # Create a Python-friendly constant name
                const_name = service_name.upper().replace(".", "_")
                f.write(f'{const_name} = "{service_name}"\n')

            f.write("\n# Proto file mappings\n")
            f.write("SERVICE_PROTOS = {\n")
            for service_name, proto_file in services:
                f.write(f'    "{service_name}": "{proto_file}",\n')
            f.write("}\n")


def main() -> int:
    """CLI entry point for generate types command."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Python types from Protocol Buffer files"
    )
    parser.add_argument(
        "--proto-dir",
        help="Directory containing .proto files",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output file path",
    )

    args = parser.parse_args()

    success = generate_types(proto_dir=args.proto_dir, output=args.output)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
