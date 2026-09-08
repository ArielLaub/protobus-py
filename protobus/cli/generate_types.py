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

import keyword
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


class GenerationError(ValueError):
    """The schema is legal protobuf but cannot be expressed as the Python
    the generator writes. Raised before any output is written."""


def _class_name(full_name: str, package: str) -> str:
    """``Combat.Player.Nested`` with ``Combat`` stripped -> ``Player_Nested``;
    a type from any other package keeps its package as a prefix. A name that
    is a Python keyword (``class``, ``from``) gets a trailing underscore, as
    PEP 8 suggests; it is a legal protobuf identifier and must still have a
    class."""
    name = full_name[len(package) + 1:] if package and full_name.startswith(package + ".") else full_name
    name = name.replace(".", "_")
    if keyword.iskeyword(name):
        name += "_"
    return name


class _Emitter:
    def __init__(self, root: Root, strip_package: str = "") -> None:
        self.root = root
        # One package is stripped from every generated name, and only when
        # every exported service lives in it: the output is a single module,
        # so ``Orders.Service`` and ``Payments.Service`` must not both become
        # ``Service``.
        self.strip_package = strip_package
        self.lines: List[str] = []
        self.emitted: Set[str] = set()
        # Generated name -> the protobuf name it came from, so two protobuf
        # names flattening onto one Python name is a refusal, not a silently
        # overwritten class.
        self.claimed: Dict[str, str] = {}
        self.uses_datetime = False
        self.uses_literal = False
        self.uses_any = False

    def field_type(self, field: FieldDescriptor, pending: List[str]) -> str:
        package = self.strip_package
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
            value_type = self.field_type(value_field, inner)
            pending.extend(inner)
            return f"Dict[{key_type}, {value_type}]"
        if _is_repeated(field):
            return f"List[{base}]"
        if field.type == FieldDescriptor.TYPE_MESSAGE:
            return f"Optional[{base}]"
        return base

    def claim(self, full_name: str) -> str:
        """The Python name for a protobuf name, refusing a collision."""
        name = _class_name(full_name, self.strip_package)
        owner = self.claimed.setdefault(name, full_name)
        if owner != full_name:
            raise GenerationError(
                f"'{full_name}' and '{owner}' would both be generated as '{name}'; "
                "rename one of them, or generate their packages separately"
            )
        return name

    def emit_type(self, full_name: str) -> None:
        if full_name in self.emitted:
            return
        self.emitted.add(full_name)
        descriptor = self.root.lookup(full_name)
        if isinstance(descriptor, EnumDescriptor):
            self.uses_literal = True
            names = ", ".join(f'"{v.name}"' for v in descriptor.values)
            self.lines.append(f"{self.claim(full_name)} = Literal[{names}]")
            self.lines.append("")
            return
        if not isinstance(descriptor, Descriptor):
            return
        if descriptor.GetOptions().map_entry:
            return
        pending: List[str] = []
        fields = [(field.name, self.field_type(field, pending)) for field in descriptor.fields]
        class_name = self.claim(full_name)
        if any(keyword.iskeyword(name) or not name.isidentifier() for name, _ in fields):
            # A field named `from` is legal protobuf and a legal dict key,
            # but not a legal attribute in a class-based TypedDict. The
            # functional form keeps the wire key exactly as declared.
            entries = ", ".join(f'"{name}": {annotation}' for name, annotation in fields)
            self.lines.append(f'{class_name} = TypedDict("{class_name}", {{{entries}}}, total=False)')
        else:
            self.lines.append(f"class {class_name}(TypedDict, total=False):")
            self.lines.extend([f"    {name}: {annotation}" for name, annotation in fields] or ["    pass"])
        self.lines.append("")
        for name in pending:
            self.emit_type(name)

    def emit_service(self, service: ServiceDescriptor) -> None:
        package = self.strip_package
        class_name = self.claim(service.full_name)
        self.lines.append(f'{class_name.upper()}_NAME = "{service.full_name}"')
        self.lines.append("")
        methods = []
        for method in service.methods:
            if keyword.iskeyword(method.name) or not method.name.isidentifier():
                # ServiceProxy installs the method under its protobuf name,
                # which Python cannot call as `proxy.from(...)` — only as
                # getattr(proxy, "from"). A Protocol cannot declare it either.
                raise GenerationError(
                    f"rpc '{service.full_name}.{method.name}' cannot be a Python method: "
                    f"'{method.name}' is a keyword. Rename the rpc in the .proto"
                )
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
            self.emit_type(method.input_type.full_name)
            self.emit_type(method.output_type.full_name)


def export_python(root: Root, service_names: List[str]) -> str:
    """Generate typing for ``service_names`` from ``root``."""
    services = [root.lookup_service(name) for name in service_names]
    packages = {service.file.package for service in services}
    emitter = _Emitter(root, packages.pop() if len(packages) == 1 else "")
    for service in services:
        emitter.emit_service(service)

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
