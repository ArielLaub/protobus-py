"""CLI configuration: a ``[tool.protobus]`` table in pyproject.toml, or defaults."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class CliConfig:
    """
    Paths the CLI works with. Keys in pyproject.toml may be spelled either
    ``proto_dir`` or ``protoDir`` (the TypeScript port's spelling).
    """

    proto_dir: str = "./proto"
    types_output: str = "./types/proto.py"
    services_dir: str = "./services"


DEFAULT_CONFIG = CliConfig()

_KEYS = {
    "proto_dir": "proto_dir", "protoDir": "proto_dir",
    "types_output": "types_output", "typesOutput": "types_output",
    "services_dir": "services_dir", "servicesDir": "services_dir",
}


def _load_toml(path: Path) -> Dict[str, Any]:
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            raise RuntimeError(
                f"reading {path} needs tomllib (Python 3.11+) or the 'tomli' package on 3.10"
            ) from None
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def load_config(cwd: Optional[str] = None, config_path: Optional[str] = None) -> CliConfig:
    """
    Load configuration from ``pyproject.toml`` in ``cwd`` (or the explicit
    ``config_path``). A missing file means defaults; a file that cannot be
    parsed is an error, not silently defaults — the settings would otherwise
    be ignored without a word.
    """
    base = Path(cwd or os.getcwd())
    path = Path(config_path) if config_path else base / "pyproject.toml"
    config = CliConfig()
    if not path.exists():
        return config
    data = _load_toml(path)
    section = data.get("tool", {}).get("protobus", {}) or {}
    for key, value in section.items():
        attr = _KEYS.get(key)
        if attr and isinstance(value, str):
            setattr(config, attr, value)
    return config


def resolve_path(config_path: str, cwd: Optional[str] = None) -> str:
    """Resolve a configured path against ``cwd``."""
    if os.path.isabs(config_path):
        return config_path
    return os.path.join(cwd or os.getcwd(), config_path)


def get_defaults() -> CliConfig:
    return CliConfig()


def find_proto_files(proto_dir: str) -> List[str]:
    """Every ``.proto`` under ``proto_dir``, recursively. Empty if absent."""
    root = Path(proto_dir)
    if not root.exists():
        return []
    return sorted(str(p) for p in root.rglob("*.proto"))
