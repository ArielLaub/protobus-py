"""The ``protobus`` command."""

import sys
from typing import List, Optional

from .. import __version__
from .generate_service import generate_service
from .generate_types import generate_types

HELP = f"""
protobus CLI v{__version__}

Usage:
  protobus generate                   Generate Python typing from .proto files
  protobus generate:service <Name>    Generate a service stub from <Name>.proto
  protobus init                       Show project setup instructions
  protobus --help                     Show this help message
  protobus --version                  Show version

Configuration:
  Add a [tool.protobus] table to pyproject.toml to customize paths:

  [tool.protobus]
  proto_dir = "./proto"
  types_output = "./types/proto.py"
  services_dir = "./services"

Examples:
  protobus generate
  protobus generate:service Calculator
"""

INIT_INSTRUCTIONS = """
Protobus Project Setup
======================

1. Create the directory structure:

   mkdir -p proto types services

2. Add configuration to your pyproject.toml:

   [tool.protobus]
   proto_dir = "./proto"
   types_output = "./types/proto.py"
   services_dir = "./services"

3. Create your first .proto file in proto/Calculator.proto:

   syntax = "proto3";
   package Calculator;

   service Service {
     rpc add(AddRequest) returns (AddResponse);
   }

   message AddRequest {
     int32 a = 1;
     int32 b = 2;
   }

   message AddResponse {
     int32 result = 1;
   }

4. Generate typing and a service stub:

   protobus generate
   protobus generate:service Calculator

5. Implement your service in services/calculator/calculator_service.py

6. Set up RabbitMQ (docker-compose.yml):

   services:
     rabbitmq:
       image: rabbitmq:3-management
       ports:
         - "5672:5672"
         - "15672:15672"

For more information, see: https://github.com/ArielLaub/protobus-py
"""


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else ""

    if not command or command in ("--help", "-h"):
        print(HELP)
        return 0
    if command in ("--version", "-v"):
        print(__version__)
        return 0
    if command == "init":
        print(INIT_INSTRUCTIONS)
        return 0
    try:
        if command == "generate":
            generate_types()
            return 0
        if command == "generate:service":
            if len(args) < 2 or not args[1]:
                print("Error: Service name required", file=sys.stderr)
                print("Usage: protobus generate:service <ServiceName>", file=sys.stderr)
                return 1
            generate_service(args[1])
            return 0
    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    print(f"Unknown command: {command}", file=sys.stderr)
    print('Run "protobus --help" for usage information', file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
