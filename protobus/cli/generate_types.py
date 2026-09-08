"""
Generate Python typing from .proto files — no protoc needed.

For every message a ``TypedDict`` (``total=False``: proto3 fields are all
optional on input), for every enum a ``Literal`` of its value names (what the
decoder produces), and for every service a ``Protocol`` whose methods carry
the exact signatures ``ServiceProxy`` installs — a unary method is an
``async def`` returning the response dict, a server-streaming one returns an
``AsyncIterator`` of chunk dicts. Plus ``SERVICE_NAME`` constants.

The counterpart of the TypeScript port's ``exportTS()`` / ``protobus generate``.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from google.protobuf.descriptor import Descriptor, EnumDescriptor, FieldDescriptor, ServiceDescriptor

from ..custom_types import get_custom_type
from ..logger import Logger
from ..message_factory import MessageFactory, Root, _custom_type_of, _is_map, _is_repeated
from .config import CliConfig, load_config, resolve_path

_SCALARS: Dict[int, str] = {
    FieldDescriptor.TYPE_DOUBLE: "float",
    FieldDescriptor.TYPE_FLOAT: "float",
    FieldDescriptor.TYPE_INT64: "int",
    FieldDescriptor.TYPE_UINT64: "int",
    FieldDescriptor.TYPE_INT32: "int",
    FieldDescriptor.TYPE_FIXED64: "int",
    FieldDescriptor.TYPE_FIXED32: "int",
    FieldDescriptor.TYPE_BOOL: "bool",
    FieldDescriptor.TYPE_STRING: "str",
    FieldDescriptor.TYPE_BYTES: "bytes",
    FieldDescriptor.TYPE_UINT32: "int",
    FieldDescriptor.TYPE_SFIXED32: "int",
    FieldDescriptor.TYPE_SFIXED64: "int",
    FieldDescriptor.TYPE_SINT32: "int",
    FieldDescriptor.TYPE_SINT64: "int",
}


def _class_name(full_name: str, package: str) -> str:
    """``Combat.Player.Nested`` in package ``Combat`` -> ``Player_Nested``;
    a type from another package keeps its package as a prefix."""
    name = full_name[len(package) + 1:] if package and full_name.startswith(package + ".") else full_name
    return name.replace(".", "_")


class _Emitter:
    def __init__(self, root: Root) -> None:
        self.root = root
        self.lines: List[str] = []
        self.emitted: Set[str] = set()
        self.uses_datetime = False
        self.uses_literal = False
        self.uses_any = False

    def field_type(self, field: FieldDescriptor, package: str, pending: List[str]) -> str:
        custom = _custom_type_of(field)
        if custom is not None:
            if custom.py_type == "datetime":
                self.uses_datetime = True
            elif custom.py_type == "Any":
                self.uses_any = True
            base = custom.py_type
        elif field.type == FieldDescriptor.TYPE_MESSAGE:
            pending.append(field.message_type.full_name)
            base = f'"{_class_name(field.message_type.full_name, package)}"'
        elif field.type == FieldDescriptor.TYPE_ENUM:
            pending.append(field.enum_type.full_name)
            base = f'"{_class_name(field.enum_type.full_name, package)}"'
        else:
            base = _SCALARS.get(field.type, "Any")
            if base == "Any":
                self.uses_any = True
        if _is_map(field):
            value_field = field.message_type.fields_by_name["value"]
            key_type = _SCALARS.get(field.message_type.fields_by_name["key"].type, "str")
            inner: List[str] = []
            value_type = self.field_type(value_field, package, inner)
            pending.extend(inner)
            return f"Dict[{key_type}, {value_type}]"
        if _is_repeated(field):
            return f"List[{base}]"
        if field.type == FieldDescriptor.TYPE_MESSAGE:
            return f"Optional[{base}]"
        return base

    def emit_type(self, full_name: str, package: str) -> None:
        if full_name in self.emitted:
            return
        self.emitted.add(full_name)
        descriptor = self.root.lookup(full_name)
        if isinstance(descriptor, EnumDescriptor):
            self.uses_literal = True
            names = ", ".join(f'"{v.name}"' for v in descriptor.values)
            self.lines.append(f"{_class_name(full_name, package)} = Literal[{names}]")
            self.lines.append("")
            return
        if not isinstance(descriptor, Descriptor):
            return
        if descriptor.GetOptions().map_entry:
            return
        pending: List[str] = []
        body = []
        for field in descriptor.fields:
            body.append(f"    {field.name}: {self.field_type(field, package, pending)}")
        self.lines.append(f"class {_class_name(full_name, package)}(TypedDict, total=False):")
        self.lines.extend(body or ["    pass"])
        self.lines.append("")
        for name in pending:
            self.emit_type(name, package)

    def emit_service(self, service: ServiceDescriptor) -> None:
        package = service.file.package
        class_name = _class_name(service.full_name, package)
        self.lines.append(f'{class_name.upper()}_NAME = "{service.full_name}"')
        self.lines.append("")
        methods = []
        for method in service.methods:
            req = _class_name(method.input_type.full_name, package)
            res = _class_name(method.output_type.full_name, package)
            if method.server_streaming:
                methods.append(
                    f"    def {method.name}(self, request: \"{req}\", actor: Optional[str] = None, "
                    f"idle_timeout_ms: Optional[int] = None, options: Optional[StreamOptions] = None) "
                    f"-> AsyncIterator[\"{res}\"]: ..."
                )
            else:
                methods.append(
                    f"    async def {method.name}(self, request: \"{req}\", actor: Optional[str] = None, "
                    f"rpc: bool = True, timeout_ms: Optional[int] = None, options: Optional[CallOptions] = None) "
                    f"-> \"{res}\": ..."
                )
        self.lines.append(f"class {class_name}(Protocol):")
        self.lines.extend(methods or ["    pass"])
        self.lines.append("")
        for method in service.methods:
            self.emit_type(method.input_type.full_name, package)
            self.emit_type(method.output_type.full_name, package)


def export_python(root: Root, service_names: List[str]) -> str:
    """Generate typing for ``service_names`` from ``root``."""
    emitter = _Emitter(root)
    for name in service_names:
        emitter.emit_service(root.lookup_service(name))

    header = [
        "# Auto-generated by protobus CLI - do not edit manually",
        "from typing import AsyncIterator, Dict, List, Optional, Protocol, TypedDict"
        + (", Literal" if emitter.uses_literal else "")
        + (", Any" if emitter.uses_any else ""),
    ]
    if emitter.uses_datetime:
        header.append("from datetime import datetime")
    header.append("from protobus import CallOptions, StreamOptions")
    header.append("")
    header.append("")
    return "\n".join(header + emitter.lines).rstrip() + "\n"


def services_in(root: Root) -> List[str]:
    """Every service declared by a user schema in ``root``."""
    names: List[str] = []
    for file_name, fdp in root.files.items():
        if file_name.startswith("google/protobuf/") or file_name.startswith("protobus/custom_types"):
            continue
        for service in fdp.service:
            names.append(f"{fdp.package}.{service.name}" if fdp.package else service.name)
    return names


def generate_types(
    config: Optional[CliConfig] = None,
    proto_dir: Optional[str] = None,
    output: Optional[str] = None,
    cwd: Optional[str] = None,
) -> str:
    """
    Generate typing for every service under the proto directory into the
    types output file. Returns the output path. Raises on any failure.
    """
    cwd = cwd or os.getcwd()
    cfg = config or load_config(cwd)
    proto_directory = resolve_path(proto_dir or cfg.proto_dir, cwd)
    types_output = resolve_path(output or cfg.types_output, cwd)

    if not os.path.isdir(proto_directory):
        raise FileNotFoundError(
            f"Proto directory not found: {proto_directory}. Create it and add your .proto files, "
            "or configure [tool.protobus] proto_dir in pyproject.toml"
        )

    factory = MessageFactory()
    factory.init([proto_directory])
    assert factory.root is not None
    service_names = services_in(factory.root)
    if not service_names:
        raise ValueError(f"No services found in the .proto files under {proto_directory}")

    print(f"Found {len(service_names)} service(s) in {proto_directory}")
    source = export_python(factory.root, service_names)
    Path(types_output).parent.mkdir(parents=True, exist_ok=True)
    with open(types_output, "w", encoding="utf-8") as fh:
        fh.write(source)
    print(f"Types generated successfully: {types_output}")
    return types_output
