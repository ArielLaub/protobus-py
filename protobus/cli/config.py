"""CLI configuration handling."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class CliConfig:
    """Configuration for Protobus CLI tools."""

    proto_dir: str = "./proto"
    types_output: str = "./types/proto.py"
    services_dir: str = "./services"


def load_config(config_path: Optional[str] = None) -> CliConfig:
    """
    Load CLI configuration from pyproject.toml or setup.cfg.

    Args:
        config_path: Optional explicit path to config file

    Returns:
        CliConfig with loaded or default values
    """
    config = CliConfig()

    # Try to load from pyproject.toml
    pyproject_path = Path(config_path) if config_path else Path("pyproject.toml")

    if pyproject_path.exists():
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                return config

        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)

        protobus_config = data.get("tool", {}).get("protobus", {})

        if "protoDir" in protobus_config:
            config.proto_dir = protobus_config["protoDir"]
        if "typesOutput" in protobus_config:
            config.types_output = protobus_config["typesOutput"]
        if "servicesDir" in protobus_config:
            config.services_dir = protobus_config["servicesDir"]

    return config


def find_proto_files(proto_dir: str) -> list[str]:
    """
    Find all .proto files in a directory.

    Args:
        proto_dir: Directory to search

    Returns:
        List of paths to .proto files
    """
    proto_path = Path(proto_dir)
    if not proto_path.exists():
        return []

    return [str(p) for p in proto_path.glob("**/*.proto")]
