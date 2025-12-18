"""Generate service stub from .proto file."""

import os
import re
import sys
from pathlib import Path
from typing import Optional

from .config import CliConfig, load_config


def generate_service(
    service_name: str,
    config: Optional[CliConfig] = None,
    output_dir: Optional[str] = None,
) -> bool:
    """
    Generate a service stub class from a service name.

    This command:
    1. Finds the .proto file for the service
    2. Extracts RPC method definitions
    3. Generates a Python class extending RunnableService

    Args:
        service_name: Full service name (e.g., "calculator.MathService")
        config: CLI configuration
        output_dir: Override output directory from config

    Returns:
        True if generation succeeded, False otherwise
    """
    cfg = config or load_config()
    services_dir = output_dir or cfg.services_dir

    print(f"Generating service stub for {service_name}...")

    # Parse service name to determine proto file
    parts = service_name.split(".")
    if len(parts) < 2:
        print(f"Error: Invalid service name format. Expected 'package.ServiceName'")
        return False

    package = ".".join(parts[:-1])
    class_name = parts[-1]
    proto_file_name = f"{parts[0]}.proto"
    proto_path = Path(cfg.proto_dir) / proto_file_name

    # Extract methods from proto file if it exists
    methods: list[dict] = []
    if proto_path.exists():
        methods = _extract_methods(proto_path, class_name)

    # Generate the service file
    output_path = Path(services_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Create filename from service name
    file_name = _to_snake_case(class_name) + "_service.py"
    service_file = output_path / file_name

    if service_file.exists():
        print(f"Warning: {service_file} already exists. Skipping.")
        print("Delete the file first if you want to regenerate.")
        return False

    # Generate the service code
    code = _generate_service_code(service_name, class_name, methods)

    with open(service_file, "w") as f:
        f.write(code)

    print(f"Service stub generated at {service_file}")
    return True


def _extract_methods(proto_path: Path, service_name: str) -> list[dict]:
    """Extract RPC methods from a proto file."""
    methods = []

    if not proto_path.exists():
        return methods

    with open(proto_path, "r") as f:
        content = f.read()

    # Find the service block
    service_pattern = rf'service\s+{re.escape(service_name)}\s*\{{([^}}]*)\}}'
    service_match = re.search(service_pattern, content, re.DOTALL)

    if not service_match:
        return methods

    service_block = service_match.group(1)

    # Extract RPC definitions
    rpc_pattern = r'rpc\s+(\w+)\s*\(\s*(\w+)\s*\)\s*returns\s*\(\s*(\w+)\s*\)'
    rpc_matches = re.findall(rpc_pattern, service_block)

    for method_name, request_type, response_type in rpc_matches:
        methods.append({
            "name": method_name,
            "request": request_type,
            "response": response_type,
        })

    return methods


def _to_snake_case(name: str) -> str:
    """Convert CamelCase to snake_case."""
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def _to_method_name(name: str) -> str:
    """Convert RPC method name to Python method name."""
    # Convert first letter to lowercase for Python convention
    if name[0].isupper():
        return name[0].lower() + name[1:]
    return name


def _generate_service_code(
    service_name: str,
    class_name: str,
    methods: list[dict],
) -> str:
    """Generate the service class code."""
    lines = [
        '"""Auto-generated service stub for ' + service_name + '."""',
        "",
        "from typing import Any, Optional",
        "",
        "from protobus import RunnableService, Context, HandledError",
        "",
        "",
        f"class {class_name}Service(RunnableService):",
        f'    """',
        f'    Service implementation for {service_name}.',
        f'    ',
        f'    TODO: Implement the RPC methods below.',
        f'    """',
        "",
        "    @property",
        "    def service_name(self) -> str:",
        f'        return "{service_name}"',
        "",
    ]

    if methods:
        for method in methods:
            method_name = _to_method_name(method["name"])
            lines.extend([
                f"    async def {method_name}(",
                "        self,",
                "        data: dict,",
                "        actor: str,",
                "        correlation_id: str,",
                "    ) -> dict:",
                f'        """',
                f'        Handle {method["name"]} RPC call.',
                f'        ',
                f'        Args:',
                f'            data: Request data ({method["request"]})',
                f'            actor: Actor identifier',
                f'            correlation_id: Request correlation ID',
                f'        ',
                f'        Returns:',
                f'            Response data ({method["response"]})',
                f'        """',
                '        # TODO: Implement this method',
                f'        raise NotImplementedError("{method_name} not implemented")',
                "",
            ])
    else:
        lines.extend([
            "    # TODO: Add your RPC methods here",
            "    # Example:",
            "    # async def my_method(",
            "    #     self,",
            "    #     data: dict,",
            "    #     actor: str,",
            "    #     correlation_id: str,",
            "    # ) -> dict:",
            "    #     return {\"result\": \"success\"}",
            "",
        ])

    lines.extend([
        "",
        "# Convenience method to start the service",
        "async def main() -> None:",
        '    """Start the service."""',
        "    import asyncio",
        "",
        "    ctx = Context()",
        '    await ctx.init("amqp://guest:guest@localhost:5672/")',
        "",
        f"    await {class_name}Service.start(ctx, {class_name}Service)",
        "",
        "",
        'if __name__ == "__main__":',
        "    import asyncio",
        "    asyncio.run(main())",
        "",
    ])

    return "\n".join(lines)


def main() -> int:
    """CLI entry point for generate service command."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a service stub from a service name"
    )
    parser.add_argument(
        "service_name",
        help="Full service name (e.g., calculator.MathService)",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        help="Output directory for generated service",
    )

    args = parser.parse_args()

    success = generate_service(args.service_name, output_dir=args.output_dir)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
