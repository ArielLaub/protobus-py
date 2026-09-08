"""
A .proto parser that produces ``FileDescriptorProto`` without ``protoc``.

The TypeScript port parses schemas at runtime with protobufjs. This module
gives the Python port the same capability with no external binary: a service
registers its own .proto text through ``MessageFactory.parse()``, and
``Context.init(proto_dirs)`` loads directories, all without ``protoc``.

Supported: proto3 (and the proto2 keywords needed to read a proto2 file —
``required``/``optional`` labels, ``default`` options are ignored), packages,
imports, nested messages, enums, oneofs, maps, ``repeated``, proto3
``optional`` (as a synthetic oneof, so presence is tracked), reserved ranges,
options (parsed and discarded, except ``allow_alias``, ``map_entry`` and
``packed`` which are carried), services with unary and ``stream`` rpcs, and
custom types (see ``custom_types``) used as if they were scalars.

Not supported: ``extend``, ``extensions``, groups, editions. Each raises a
ProtoParseError naming the construct.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from google.protobuf import descriptor_pb2

from .custom_types import get_custom_type_names, is_custom_type

FDP = descriptor_pb2.FieldDescriptorProto

SCALAR_TYPES: Dict[str, int] = {
    "double": FDP.TYPE_DOUBLE,
    "float": FDP.TYPE_FLOAT,
    "int64": FDP.TYPE_INT64,
    "uint64": FDP.TYPE_UINT64,
    "int32": FDP.TYPE_INT32,
    "fixed64": FDP.TYPE_FIXED64,
    "fixed32": FDP.TYPE_FIXED32,
    "bool": FDP.TYPE_BOOL,
    "string": FDP.TYPE_STRING,
    "bytes": FDP.TYPE_BYTES,
    "uint32": FDP.TYPE_UINT32,
    "sfixed32": FDP.TYPE_SFIXED32,
    "sfixed64": FDP.TYPE_SFIXED64,
    "sint32": FDP.TYPE_SINT32,
    "sint64": FDP.TYPE_SINT64,
}

#: The synthetic file every parsed schema depends on for its custom types.
CUSTOM_TYPES_FILE = "protobus/custom_types.proto"


class ProtoParseError(Exception):
    """The .proto text could not be parsed."""

    def __init__(self, message: str, filename: str = "", line: int = 0):
        where = f"({filename or 'schema'}, line {line})" if line else f"({filename or 'schema'})"
        super().__init__(f"{message} {where}")
        self.filename = filename
        self.line = line


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<line_comment>//[^\n]*)
  | (?P<block_comment>/\*.*?\*/)
  | (?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<number>[-+]?(?:0[xX][0-9a-fA-F]+|\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|\d+(?:[eE][-+]?\d+)?|inf|nan))
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*|\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
  | (?P<punct>[{}()\[\]<>=;,:.\-+])
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass
class Token:
    kind: str  # 'string' | 'number' | 'ident' | 'punct' | 'eof'
    value: str
    line: int


def tokenize(text: str, filename: str = "") -> List[Token]:
    tokens: List[Token] = []
    pos = 0
    line = 1
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ProtoParseError(f"unexpected character {text[pos]!r}", filename, line)
        kind = m.lastgroup or ""
        value = m.group(0)
        if kind in ("ws", "line_comment", "block_comment"):
            line += value.count("\n")
        elif kind == "string":
            tokens.append(Token("string", _unquote(value), line))
        else:
            tokens.append(Token(kind, value, line))
        pos = m.end()
    tokens.append(Token("eof", "", line))
    return tokens


def _unquote(literal: str) -> str:
    body = literal[1:-1]
    return bytes(body, "utf-8").decode("unicode_escape") if "\\" in body else body


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@dataclass
class _PendingField:
    """A field whose type is a name to resolve once every declaration is known."""

    field: descriptor_pb2.FieldDescriptorProto
    type_name: str
    scope: str  # full name of the enclosing message ('' at file level)
    line: int


@dataclass
class _PendingMethod:
    method: descriptor_pb2.MethodDescriptorProto
    input_name: str
    output_name: str
    scope: str
    line: int


TypeLookup = Callable[[str], Optional[Tuple[str, str]]]
"""
Given a fully-qualified name (no leading dot), answer ``(kind, file_name)``
where kind is 'message' or 'enum' and file_name is the file declaring it, or
None when the name is unknown.
"""


class ProtoParser:
    """
    Parse one .proto source into a ``FileDescriptorProto``.

    ``lookup`` answers whether a fully-qualified name is already known to the
    caller's descriptor pool (imported files, custom types), so references
    into other files resolve. Names declared by this file are resolved first.
    """

    def __init__(
        self,
        text: str,
        filename: str = "",
        lookup: Optional[TypeLookup] = None,
    ):
        self.text = text
        self.filename = filename
        self.lookup = lookup or (lambda _name: None)
        self.tokens = tokenize(text, filename)
        self.pos = 0
        self.file = descriptor_pb2.FileDescriptorProto()
        self.file.name = filename or f"protobus/{hashlib.sha1(text.encode('utf-8')).hexdigest()}.proto"
        self.package = ""
        self.syntax = "proto2"
        # Every message and enum this file declares, by fully-qualified name.
        self.declared: Dict[str, str] = {}
        # Files whose symbols this schema uses, derived from actual references
        # rather than trusted from `import` lines: an import that is missing
        # is the difference between a schema that loads and one that does not.
        self.used_files: Set[str] = set()
        self.pending_fields: List[_PendingField] = []
        self.pending_methods: List[_PendingMethod] = []

    # -- token helpers ------------------------------------------------------

    def _peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def _next(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.kind != "eof":
            self.pos += 1
        return tok

    def _error(self, message: str, tok: Optional[Token] = None) -> ProtoParseError:
        tok = tok or self._peek()
        return ProtoParseError(message, self.filename, tok.line)

    def _expect(self, value: str) -> Token:
        tok = self._next()
        if tok.value != value:
            raise self._error(f"expected {value!r}, got {tok.value!r}", tok)
        return tok

    def _accept(self, value: str) -> bool:
        if self._peek().value == value:
            self._next()
            return True
        return False

    def _ident(self) -> Token:
        tok = self._next()
        if tok.kind != "ident":
            raise self._error(f"expected an identifier, got {tok.value!r}", tok)
        return tok

    def _simple_ident(self) -> Token:
        tok = self._ident()
        if "." in tok.value:
            raise self._error(f"expected a simple identifier, got {tok.value!r}", tok)
        return tok

    def _int(self) -> int:
        tok = self._next()
        if tok.kind != "number":
            raise self._error(f"expected a number, got {tok.value!r}", tok)
        try:
            return int(tok.value, 0)
        except ValueError:
            raise self._error(f"expected an integer, got {tok.value!r}", tok) from None

    def _constant(self) -> str:
        """An option value: string, number, identifier, bool or aggregate."""
        tok = self._peek()
        if tok.value == "{":
            depth = 0
            parts = []
            while True:
                t = self._next()
                if t.kind == "eof":
                    raise self._error("unterminated aggregate option")
                parts.append(t.value)
                if t.value == "{":
                    depth += 1
                elif t.value == "}":
                    depth -= 1
                    if depth == 0:
                        return " ".join(parts)
        if tok.value in ("-", "+"):
            self._next()
            return tok.value + self._next().value
        return self._next().value

    # -- file level ---------------------------------------------------------

    def parse(self) -> descriptor_pb2.FileDescriptorProto:
        while self._peek().kind != "eof":
            tok = self._peek()
            if tok.value == ";":
                self._next()
            elif tok.value == "syntax":
                self._parse_syntax()
            elif tok.value == "edition":
                raise self._error("protobuf editions are not supported")
            elif tok.value == "package":
                self._parse_package()
            elif tok.value == "import":
                self._parse_import()
            elif tok.value == "option":
                self._parse_option(self.file.options)
            elif tok.value == "message":
                self._parse_message(None, self.package)
            elif tok.value == "enum":
                self._parse_enum(None, self.package)
            elif tok.value == "service":
                self._parse_service()
            elif tok.value in ("extend", "extensions", "group"):
                raise self._error(f"'{tok.value}' is not supported")
            else:
                raise self._error(f"unexpected {tok.value!r} at file level")

        self._resolve()
        # Every file a reference resolved into is a dependency, whether or not
        # the schema said `import`. Custom types live in a synthetic
        # package-less file, so `bigint` arrives here like any imported type.
        for used in sorted(self.used_files):
            if used != self.file.name and used not in self.file.dependency:
                self.file.dependency.append(used)
        return self.file

    def _parse_syntax(self) -> None:
        self._expect("syntax")
        self._expect("=")
        tok = self._next()
        if tok.kind != "string" or tok.value not in ("proto2", "proto3"):
            raise self._error(f"unsupported syntax {tok.value!r}", tok)
        self.syntax = tok.value
        if tok.value == "proto3":
            self.file.syntax = "proto3"
        self._expect(";")

    def _parse_package(self) -> None:
        self._expect("package")
        tok = self._ident()
        if tok.value.startswith("."):
            raise self._error("package name must not start with '.'", tok)
        self.package = tok.value
        self.file.package = tok.value
        self._expect(";")

    def _parse_import(self) -> None:
        self._expect("import")
        visibility = None
        if self._peek().value in ("public", "weak"):
            visibility = self._next().value
        tok = self._next()
        if tok.kind != "string":
            raise self._error("expected a file name after 'import'", tok)
        self._expect(";")
        self.file.dependency.append(tok.value)
        if visibility == "public":
            self.file.public_dependency.append(len(self.file.dependency) - 1)
        elif visibility == "weak":
            self.file.weak_dependency.append(len(self.file.dependency) - 1)

    def _parse_option(self, options_message) -> None:  # noqa: ANN001
        """``option name = value;`` — parsed and, for a few known names, kept."""
        self._expect("option")
        name, value = self._parse_option_body()
        self._expect(";")
        self._apply_option(options_message, name, value)

    def _parse_option_body(self) -> Tuple[str, str]:
        if self._accept("("):
            name = self._ident().value
            self._expect(")")
            while self._peek().value == ".":
                self._next()
                name += "." + self._ident().value
        else:
            name = self._ident().value
        self._expect("=")
        return name, self._constant()

    def _apply_option(self, options_message, name: str, value: str) -> None:  # noqa: ANN001
        if options_message is None:
            return
        bool_options = {"allow_alias", "map_entry", "packed", "deprecated"}
        if name in bool_options and hasattr(options_message, name):
            setattr(options_message, name, value == "true")
        elif name == "java_package" and hasattr(options_message, name):
            options_message.java_package = value
        # Everything else is ignored: it has no effect on the wire.

    # -- messages -----------------------------------------------------------

    def _qualify(self, scope: str, name: str) -> str:
        return f"{scope}.{name}" if scope else name

    def _parse_message(
        self, parent: Optional[descriptor_pb2.DescriptorProto], scope: str
    ) -> descriptor_pb2.DescriptorProto:
        self._expect("message")
        name_tok = self._simple_ident()
        message = (parent.nested_type if parent is not None else self.file.message_type).add()
        message.name = name_tok.value
        full_name = self._qualify(scope, name_tok.value)
        if full_name in self.declared:
            raise self._error(f"duplicate name {full_name!r}", name_tok)
        self.declared[full_name] = "message"
        self._parse_message_body(message, full_name)
        return message

    def _parse_message_body(self, message: descriptor_pb2.DescriptorProto, full_name: str) -> None:
        self._expect("{")
        # proto3 `optional` fields get a synthetic oneof each, and the
        # descriptor rules require every synthetic oneof to come after every
        # declared one — so they are added once the body has been read.
        proto3_optional: List[descriptor_pb2.FieldDescriptorProto] = []
        while True:
            tok = self._peek()
            if tok.kind == "eof":
                raise self._error(f"unterminated message {message.name!r}", tok)
            if tok.value == "}":
                self._next()
                for fld in proto3_optional:
                    oneof = message.oneof_decl.add()
                    oneof.name = f"_{fld.name}"
                    fld.oneof_index = len(message.oneof_decl) - 1
                    fld.proto3_optional = True
                return
            if tok.value == ";":
                self._next()
            elif tok.value == "message":
                self._parse_message(message, full_name)
            elif tok.value == "enum":
                self._parse_enum(message, full_name)
            elif tok.value == "option":
                self._parse_option(message.options)
            elif tok.value == "oneof":
                self._parse_oneof(message, full_name)
            elif tok.value == "reserved":
                self._parse_reserved(message)
            elif tok.value == "map" and self._peek(1).value == "<":
                self._parse_map_field(message, full_name)
            elif tok.value in ("extensions", "extend", "group"):
                raise self._error(f"'{tok.value}' is not supported", tok)
            else:
                fld = self._parse_field(message, full_name, oneof_index=None)
                if fld is not None:
                    proto3_optional.append(fld)

    def _parse_reserved(self, message: descriptor_pb2.DescriptorProto) -> None:
        self._expect("reserved")
        while True:
            tok = self._peek()
            if tok.kind == "string":
                self._next()
                message.reserved_name.append(tok.value)
            elif tok.kind == "number":
                start = self._int()
                end = start
                if self._accept("to"):
                    if self._peek().value == "max":
                        self._next()
                        end = 536870911
                    else:
                        end = self._int()
                message.reserved_range.add(start=start, end=end + 1)
            elif tok.kind == "ident" and self._peek(1).value in (",", ";"):
                # proto editions allow bare identifiers; tolerate them.
                self._next()
                message.reserved_name.append(tok.value)
            else:
                raise self._error("expected a reserved name or range", tok)
            if not self._accept(","):
                break
        self._expect(";")

    def _parse_field_options(self, fld: descriptor_pb2.FieldDescriptorProto) -> None:
        if not self._accept("["):
            return
        while True:
            name, value = self._parse_option_body()
            if name == "packed":
                fld.options.packed = value == "true"
            elif name == "deprecated":
                fld.options.deprecated = value == "true"
            elif name == "default":
                fld.default_value = value
            elif name == "json_name":
                fld.json_name = value
            if not self._accept(","):
                break
        self._expect("]")

    def _parse_field(
        self,
        message: descriptor_pb2.DescriptorProto,
        scope: str,
        oneof_index: Optional[int],
    ) -> Optional[descriptor_pb2.FieldDescriptorProto]:
        """Parse one field. Returns the field if it is a proto3 `optional`."""
        label = FDP.LABEL_OPTIONAL
        proto3_optional = False
        tok = self._peek()
        if tok.value == "repeated":
            self._next()
            label = FDP.LABEL_REPEATED
        elif tok.value == "optional":
            self._next()
            if self.syntax == "proto3":
                proto3_optional = True
        elif tok.value == "required":
            self._next()
            label = FDP.LABEL_REQUIRED

        if oneof_index is not None and label != FDP.LABEL_OPTIONAL:
            raise self._error("fields in a oneof cannot be repeated or required", tok)

        type_tok = self._ident()
        name_tok = self._simple_ident()
        self._expect("=")
        number = self._int()

        fld = message.field.add()
        fld.name = name_tok.value
        fld.number = number
        fld.label = label
        fld.json_name = _json_name(name_tok.value)
        self._set_type(fld, type_tok.value, scope, type_tok.line)
        self._parse_field_options(fld)
        self._expect(";")

        if oneof_index is not None:
            fld.oneof_index = oneof_index
            return None
        return fld if proto3_optional else None

    def _parse_map_field(self, message: descriptor_pb2.DescriptorProto, scope: str) -> None:
        self._expect("map")
        self._expect("<")
        key_tok = self._ident()
        self._expect(",")
        value_tok = self._ident()
        self._expect(">")
        name_tok = self._simple_ident()
        self._expect("=")
        number = self._int()

        if key_tok.value not in SCALAR_TYPES or key_tok.value in ("float", "double", "bytes"):
            raise self._error(f"invalid map key type {key_tok.value!r}", key_tok)

        entry_name = _map_entry_name(name_tok.value)
        entry = message.nested_type.add()
        entry.name = entry_name
        entry.options.map_entry = True
        key = entry.field.add(name="key", number=1, label=FDP.LABEL_OPTIONAL, json_name="key")
        key.type = SCALAR_TYPES[key_tok.value]
        value = entry.field.add(name="value", number=2, label=FDP.LABEL_OPTIONAL, json_name="value")
        entry_full = self._qualify(scope, entry_name)
        self.declared[entry_full] = "message"
        self._set_type(value, value_tok.value, entry_full, value_tok.line)

        fld = message.field.add()
        fld.name = name_tok.value
        fld.number = number
        fld.label = FDP.LABEL_REPEATED
        fld.type = FDP.TYPE_MESSAGE
        fld.type_name = "." + entry_full
        fld.json_name = _json_name(name_tok.value)
        self._parse_field_options(fld)
        self._expect(";")

    def _set_type(self, fld: descriptor_pb2.FieldDescriptorProto, type_name: str, scope: str, line: int) -> None:
        if type_name in SCALAR_TYPES:
            fld.type = SCALAR_TYPES[type_name]
            return
        self.pending_fields.append(_PendingField(fld, type_name, scope, line))

    def _parse_oneof(self, message: descriptor_pb2.DescriptorProto, scope: str) -> None:
        self._expect("oneof")
        name_tok = self._simple_ident()
        oneof = message.oneof_decl.add()
        oneof.name = name_tok.value
        index = len(message.oneof_decl) - 1
        self._expect("{")
        while True:
            tok = self._peek()
            if tok.kind == "eof":
                raise self._error(f"unterminated oneof {name_tok.value!r}", tok)
            if tok.value == "}":
                self._next()
                return
            if tok.value == ";":
                self._next()
            elif tok.value == "option":
                self._parse_option(None)
            elif tok.value == "group":
                raise self._error("groups are not supported", tok)
            else:
                self._parse_field(message, scope, oneof_index=index)

    # -- enums --------------------------------------------------------------

    def _parse_enum(self, parent: Optional[descriptor_pb2.DescriptorProto], scope: str) -> None:
        self._expect("enum")
        name_tok = self._simple_ident()
        enum = (parent.enum_type if parent is not None else self.file.enum_type).add()
        enum.name = name_tok.value
        full_name = self._qualify(scope, name_tok.value)
        if full_name in self.declared:
            raise self._error(f"duplicate name {full_name!r}", name_tok)
        self.declared[full_name] = "enum"

        self._expect("{")
        while True:
            tok = self._peek()
            if tok.kind == "eof":
                raise self._error(f"unterminated enum {enum.name!r}", tok)
            if tok.value == "}":
                self._next()
                break
            if tok.value == ";":
                self._next()
            elif tok.value == "option":
                self._parse_option(enum.options)
            elif tok.value == "reserved":
                self._parse_enum_reserved(enum)
            else:
                value_tok = self._simple_ident()
                self._expect("=")
                negative = self._accept("-")
                number = self._int()
                value = enum.value.add()
                value.name = value_tok.value
                value.number = -number if negative else number
                if self._accept("["):
                    while True:
                        name, opt = self._parse_option_body()
                        if name == "deprecated":
                            value.options.deprecated = opt == "true"
                        if not self._accept(","):
                            break
                    self._expect("]")
                self._expect(";")

        if self.syntax == "proto3" and enum.value and enum.value[0].number != 0:
            raise self._error(f"the first value of proto3 enum {enum.name!r} must be zero", name_tok)

    def _parse_enum_reserved(self, enum: descriptor_pb2.EnumDescriptorProto) -> None:
        self._expect("reserved")
        while True:
            tok = self._peek()
            if tok.kind == "string":
                self._next()
                enum.reserved_name.append(tok.value)
            else:
                negative = self._accept("-")
                start = self._int()
                start = -start if negative else start
                end = start
                if self._accept("to"):
                    if self._peek().value == "max":
                        self._next()
                        end = 2147483647
                    else:
                        neg = self._accept("-")
                        end = self._int()
                        end = -end if neg else end
                enum.reserved_range.add(start=start, end=end)
            if not self._accept(","):
                break
        self._expect(";")

    # -- services -----------------------------------------------------------

    def _parse_service(self) -> None:
        self._expect("service")
        name_tok = self._simple_ident()
        service = self.file.service.add()
        service.name = name_tok.value
        full_name = self._qualify(self.package, name_tok.value)
        if full_name in self.declared:
            raise self._error(f"duplicate name {full_name!r}", name_tok)
        self.declared[full_name] = "service"

        self._expect("{")
        while True:
            tok = self._peek()
            if tok.kind == "eof":
                raise self._error(f"unterminated service {service.name!r}", tok)
            if tok.value == "}":
                self._next()
                return
            if tok.value == ";":
                self._next()
            elif tok.value == "option":
                self._parse_option(service.options)
            elif tok.value == "rpc":
                self._parse_rpc(service)
            else:
                raise self._error(f"unexpected {tok.value!r} in service {service.name!r}", tok)

    def _parse_rpc(self, service: descriptor_pb2.ServiceDescriptorProto) -> None:
        self._expect("rpc")
        name_tok = self._simple_ident()
        method = service.method.add()
        method.name = name_tok.value

        self._expect("(")
        if self._accept("stream"):
            method.client_streaming = True
        input_tok = self._ident()
        self._expect(")")
        self._expect("returns")
        self._expect("(")
        if self._accept("stream"):
            method.server_streaming = True
        output_tok = self._ident()
        self._expect(")")

        if self._accept("{"):
            while True:
                tok = self._peek()
                if tok.kind == "eof":
                    raise self._error(f"unterminated rpc {method.name!r}", tok)
                if tok.value == "}":
                    self._next()
                    break
                if tok.value == ";":
                    self._next()
                elif tok.value == "option":
                    self._parse_option(method.options)
                else:
                    raise self._error(f"unexpected {tok.value!r} in rpc {method.name!r}", tok)
        else:
            self._expect(";")

        self.pending_methods.append(
            _PendingMethod(method, input_tok.value, output_tok.value, self.package, name_tok.line)
        )

    # -- resolution ---------------------------------------------------------

    def _kind_of(self, full_name: str) -> Optional[str]:
        """'message' / 'enum' / 'service' for a fully-qualified name, or None."""
        kind = self.declared.get(full_name)
        if kind:
            return kind
        found = self.lookup(full_name)
        if found:
            kind, file_name = found
            self.used_files.add(file_name)
            return kind
        return None

    def _resolve_name(self, name: str, scope: str) -> Optional[Tuple[str, str]]:
        """
        Resolve a type reference the way protoc does: an absolute name is
        looked up as-is; a relative one is searched from the innermost
        enclosing scope outwards. Returns (kind, full_name) or None.
        """
        if name.startswith("."):
            full = name[1:]
            kind = self._kind_of(full)
            return (kind, full) if kind else None

        # Innermost scope first, then each enclosing one, then the root.
        current = scope
        while True:
            candidate = f"{current}.{name}" if current else name
            kind = self._kind_of(candidate)
            if kind:
                return kind, candidate
            if not current:
                return None
            current = current.rpartition(".")[0]

    def _resolve(self) -> None:
        for pending in self.pending_fields:
            resolved = self._resolve_name(pending.type_name, pending.scope)
            if resolved is None and is_custom_type(pending.type_name):
                # A custom scalar the pool does not hold yet. Carried as the
                # wrapper message in the synthetic custom-types file.
                self.used_files.add(CUSTOM_TYPES_FILE)
                pending.field.type = FDP.TYPE_MESSAGE
                pending.field.type_name = "." + pending.type_name
                continue
            if resolved is None:
                known = ", ".join(sorted(get_custom_type_names()))
                raise ProtoParseError(
                    f"unknown type {pending.type_name!r} (not a scalar, not declared in "
                    f"this file or an imported one, and not a registered custom type: {known})",
                    self.filename,
                    pending.line,
                )
            kind, full = resolved
            if kind == "message":
                pending.field.type = FDP.TYPE_MESSAGE
            elif kind == "enum":
                pending.field.type = FDP.TYPE_ENUM
            else:
                raise ProtoParseError(
                    f"{pending.type_name!r} is a {kind}, not a message or enum",
                    self.filename,
                    pending.line,
                )
            pending.field.type_name = "." + full

        for pending in self.pending_methods:
            for attr, name in (("input_type", pending.input_name), ("output_type", pending.output_name)):
                resolved = self._resolve_name(name, pending.scope)
                if resolved is None or resolved[0] != "message":
                    raise ProtoParseError(
                        f"rpc {pending.method.name!r} refers to unknown message type {name!r}",
                        self.filename,
                        pending.line,
                    )
                setattr(pending.method, attr, "." + resolved[1])


def _json_name(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _map_entry_name(field_name: str) -> str:
    """protoc's rule: capitalise, strip underscores, append Entry."""
    return "".join(p[:1].upper() + p[1:] for p in field_name.split("_")) + "Entry"


def parse_proto(
    text: str,
    filename: str = "",
    lookup: Optional[TypeLookup] = None,
) -> descriptor_pb2.FileDescriptorProto:
    """Parse ``text`` into a ``FileDescriptorProto``. See ProtoParser."""
    return ProtoParser(text, filename, lookup).parse()


def custom_types_file() -> descriptor_pb2.FileDescriptorProto:
    """
    The synthetic file declaring every registered custom type's wrapper
    message, at the root (no package), so a field of type ``bigint`` in any
    package resolves to ``.bigint`` — the same name protobufjs gives it.
    """
    from .custom_types import get_custom_type, wrapper_descriptor

    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.name = CUSTOM_TYPES_FILE
    fdp.syntax = "proto3"
    for name in get_custom_type_names():
        custom = get_custom_type(name)
        if custom is not None:
            fdp.message_type.append(wrapper_descriptor(custom))
    return fdp


def iter_declared_names(fdp: descriptor_pb2.FileDescriptorProto) -> Iterator[Tuple[str, str]]:
    """Every (full_name, kind) a file declares: messages, enums, services."""

    def walk(message: descriptor_pb2.DescriptorProto, scope: str) -> Iterator[Tuple[str, str]]:
        full = f"{scope}.{message.name}" if scope else message.name
        yield full, "message"
        for nested in message.nested_type:
            yield from walk(nested, full)
        for enum in message.enum_type:
            yield f"{full}.{enum.name}", "enum"

    for message in fdp.message_type:
        yield from walk(message, fdp.package)
    for enum in fdp.enum_type:
        yield (f"{fdp.package}.{enum.name}" if fdp.package else enum.name), "enum"
    for service in fdp.service:
        yield (f"{fdp.package}.{service.name}" if fdp.package else service.name), "service"
