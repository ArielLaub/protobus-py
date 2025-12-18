# CLI Tools

Protobus includes command-line tools for generating Python types from Protocol Buffer definitions and scaffolding service implementations.

## Installation

The CLI is included when you install protobus:

```bash
pip install protobus
```

## Running the CLI

After installation, run commands directly:

```bash
protobus generate
protobus generate:service calculator.MathService
```

**Without installing** (like `npx` in Node.js):

```bash
# Using pipx (recommended)
pipx run protobus generate

# Or from source
python -m protobus.cli.main generate
```

## Commands

### `protobus generate`

Generates Python type definitions from all `.proto` files in the configured directory.

```bash
protobus generate [--proto-dir DIR] [--output FILE]
```

**Options:**
- `--proto-dir`: Directory containing .proto files (default: from config)
- `--output`, `-o`: Output file path (default: from config)

**What it does:**
1. Finds all `.proto` files in the proto directory
2. Uses `protoc` to generate Python code with type stubs
3. Creates a service constants file with `ServiceName` values

**Example:**
```bash
# Use config from pyproject.toml
protobus generate

# Override proto directory
protobus generate --proto-dir ./protos

# Specify output location
protobus generate -o ./types/generated.py
```

### `protobus generate:service`

Generates a service stub class from a service name.

```bash
protobus generate:service <service_name> [--output-dir DIR]
```

**Arguments:**
- `service_name`: Full service name (e.g., `calculator.MathService`)

**Options:**
- `--output-dir`, `-o`: Output directory for generated service

**What it does:**
1. Parses the corresponding `.proto` file (if it exists)
2. Extracts RPC method definitions
3. Generates a Python class extending `RunnableService`
4. Creates method stubs ready for implementation

**Example:**
```bash
# Generate a calculator service
protobus generate:service calculator.MathService

# Specify output directory
protobus generate:service calculator.MathService -o ./services
```

**Generated code:**
```python
"""Auto-generated service stub for calculator.MathService."""

from protobus import RunnableService, Context, HandledError


class MathServiceService(RunnableService):
    """Service implementation for calculator.MathService."""

    @property
    def service_name(self) -> str:
        return "calculator.MathService"

    async def add(
        self,
        data: dict,
        actor: str,
        correlation_id: str,
    ) -> dict:
        """Handle Add RPC call."""
        # TODO: Implement this method
        raise NotImplementedError("add not implemented")
```

### `protobus init`

Displays setup instructions for a new Protobus project.

```bash
protobus init
```

## Configuration

Configure CLI options in your `pyproject.toml`:

```toml
[tool.protobus]
protoDir = "./proto"
typesOutput = "./types/proto.py"
servicesDir = "./services"
```

**Options:**
- `protoDir`: Directory containing .proto files
- `typesOutput`: Output file for generated type definitions
- `servicesDir`: Output directory for generated service stubs

## Workflow

A typical development workflow:

1. **Create .proto files** in your proto directory:
   ```protobuf
   // proto/calculator.proto
   syntax = "proto3";
   package calculator;

   service MathService {
     rpc Add(AddRequest) returns (AddResponse);
     rpc Multiply(MultiplyRequest) returns (MultiplyResponse);
   }

   message AddRequest {
     int32 a = 1;
     int32 b = 2;
   }

   message AddResponse {
     int32 result = 1;
   }
   ```

2. **Generate types:**
   ```bash
   protobus generate
   ```

3. **Generate service stubs:**
   ```bash
   protobus generate:service calculator.MathService
   ```

4. **Implement the generated methods**

5. **Run your service:**
   ```bash
   python services/math_service_service.py
   ```

## Requirements

- **protoc**: The Protocol Buffers compiler must be installed
  - macOS: `brew install protobuf`
  - Ubuntu: `apt install protobuf-compiler`
  - Windows: `choco install protoc`

## Tips

- Add `protobus generate` to your build pipeline to keep generated code synchronized
- Use the generated `ServiceName` constants instead of hardcoding service identifiers
- Service generation won't overwrite existing files - delete first if regeneration is needed
