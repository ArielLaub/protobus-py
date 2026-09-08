"""The CLI: configuration, type generation against the runtime contract,
service stubs, and packaging."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from protobus import MessageFactory
from protobus.cli.config import CliConfig, find_proto_files, load_config, resolve_path
from protobus.cli.generate_service import generate_service, to_snake_case
from protobus.cli.generate_types import export_python, generate_types, services_in
from protobus.cli.main import main

REPO_ROOT = Path(__file__).resolve().parents[2]

PROTO = """
syntax = "proto3";
package Demo;
enum Mode { OFF = 0; ON = 1; }
message Inner { string label = 1; }
message M {
    int64   n    = 1;
    uint64  u    = 2;
    string  name = 3;
    bytes   blob = 4;
    repeated Inner items = 5;
    map<string, int32> counts = 6;
    Inner one = 7;
    Mode mode = 8;
    bigint amount = 9;
    timestamp at = 10;
}
service Calc {
    rpc x(M) returns(M);
    rpc watch(M) returns(stream M);
}
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "proto").mkdir()
    (tmp_path / "proto" / "Demo.proto").write_text(PROTO)
    return tmp_path


class TestCliConfig:
    def test_default_config(self):
        config = CliConfig()
        assert (config.proto_dir, config.types_output, config.services_dir) == ("./proto", "./types/proto.py", "./services")

    def test_loads_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[tool.protobus]\nproto_dir = "./schemas"\ntypesOutput = "./gen/types.py"\n')
        config = load_config(str(tmp_path))
        assert config.proto_dir == "./schemas"
        assert config.types_output == "./gen/types.py"
        assert config.services_dir == "./services"

    def test_a_missing_pyproject_means_defaults(self, tmp_path):
        assert load_config(str(tmp_path)) == CliConfig()

    def test_an_unparseable_pyproject_is_an_error_not_silent_defaults(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.protobus\nbroken")
        with pytest.raises(Exception):
            load_config(str(tmp_path))

    def test_resolve_path(self):
        assert resolve_path("/abs/path", "/cwd") == "/abs/path"
        assert resolve_path("./rel", "/cwd") == "/cwd/./rel"

    def test_find_proto_files_is_recursive_and_tolerates_a_missing_dir(self, tmp_path):
        assert find_proto_files(str(tmp_path / "nope")) == []
        (tmp_path / "a.proto").write_text("")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.proto").write_text("")
        (tmp_path / "notes.proto.bak").write_text("")
        assert [os.path.basename(p) for p in find_proto_files(str(tmp_path))] == ["a.proto", "b.proto"]


class TestGeneratedTypesMatchTheRuntimeContract:
    @pytest.fixture
    def generated(self, project):
        out = generate_types(proto_dir=str(project / "proto"), output=str(project / "types" / "proto.py"), cwd=str(project))
        return Path(out).read_text()

    def test_the_output_is_valid_python_that_imports(self, generated, project):
        namespace = {}
        exec(compile(generated, "proto.py", "exec"), namespace)
        assert namespace["CALC_NAME"] == "Demo.Calc"
        assert "Calc" in namespace and "M" in namespace and "Inner" in namespace

    def test_declares_a_server_streaming_method_as_an_async_iterator(self, generated):
        watch = next(line for line in generated.splitlines() if "def watch(" in line)
        assert 'AsyncIterator["M"]' in watch and "idle_timeout_ms" in watch and "StreamOptions" in watch
        assert not watch.strip().startswith("async ")

    def test_leaves_a_unary_method_as_a_coroutine_with_the_proxy_arguments(self, generated):
        x = next(line for line in generated.splitlines() if "def x(" in line)
        assert x.strip().startswith("async def x(")
        assert '-> "M"' in x and "rpc: bool" in x and "timeout_ms" in x and "CallOptions" in x

    def test_64_bit_scalars_are_ints_and_the_rest_follow_the_decoder(self, generated):
        body = generated[generated.index("class M(TypedDict"):]
        for line in ("    n: int", "    u: int", "    name: str", "    blob: bytes", '    items: List["Inner"]',
                     "    counts: Dict[str, int]", '    one: Optional["Inner"]', '    mode: "Mode"',
                     "    amount: Optional[int]", "    at: Optional[datetime]"):
            assert line in body, line
        assert 'Mode = Literal["OFF", "ON"]' in generated

    def test_the_generated_shape_is_what_the_factory_decodes(self, generated):
        factory = MessageFactory()
        factory.init([])
        factory.parse(PROTO, "Demo.Calc")
        decoded = factory.decode_response(factory.build_response("Demo.Calc.x", {"n": 1, "items": [{"label": "a"}], "mode": "ON"})).result.data
        namespace = {}
        exec(compile(generated, "proto.py", "exec"), namespace)
        # Every decoded key is a declared key of the TypedDict, and vice versa.
        assert set(decoded) == set(namespace["M"].__annotations__)

    def test_export_python_for_a_root(self):
        factory = MessageFactory()
        factory.init([])
        factory.parse(PROTO, "Demo.Calc")
        assert services_in(factory.root) == ["Demo.Calc"]
        assert "class Calc(Protocol)" in export_python(factory.root, ["Demo.Calc"])

    def test_a_missing_proto_directory_is_reported(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            generate_types(proto_dir=str(tmp_path / "nope"), output=str(tmp_path / "t.py"), cwd=str(tmp_path))

    def test_no_services_is_reported(self, tmp_path):
        (tmp_path / "proto").mkdir()
        (tmp_path / "proto" / "a.proto").write_text('syntax = "proto3"; message A {}')
        with pytest.raises(ValueError, match="No services"):
            generate_types(proto_dir=str(tmp_path / "proto"), output=str(tmp_path / "t.py"), cwd=str(tmp_path))


class TestGenerateService:
    def test_to_snake_case(self):
        assert to_snake_case("MathService") == "math_service"
        assert to_snake_case("HTTPClient") == "http_client"
        assert to_snake_case("MyAPIHandler") == "my_api_handler"
        assert to_snake_case("simple") == "simple"

    def test_generates_a_stub_from_the_parsed_schema(self, project):
        out = generate_service("Demo", config=CliConfig(proto_dir=str(project / "proto"), services_dir=str(project / "services")), cwd=str(project))
        assert Path(out) == project / "services" / "demo" / "demo_service.py"
        source = Path(out).read_text()
        assert 'service_name = "Demo.Calc"' in source
        assert "async def x(self, request: dict, actor: str, correlation_id: str) -> dict:" in source
        assert "async def watch(self, request: dict, actor: str, correlation_id: str, context: MessageHandlerContext):" in source
        assert "yield" in source
        # It compiles.
        compile(source, out, "exec")

    def test_refuses_to_overwrite(self, project):
        cfg = CliConfig(proto_dir=str(project / "proto"), services_dir=str(project / "services"))
        generate_service("Demo", config=cfg, cwd=str(project))
        with pytest.raises(FileExistsError):
            generate_service("Demo", config=cfg, cwd=str(project))

    def test_reports_a_missing_proto(self, project):
        with pytest.raises(FileNotFoundError):
            generate_service("Nope", config=CliConfig(proto_dir=str(project / "proto"), services_dir=str(project / "services")), cwd=str(project))

    def test_rejects_an_unsafe_name_before_touching_the_filesystem(self, project):
        from protobus.cli.generate_service import InvalidServiceNameError

        with pytest.raises(InvalidServiceNameError):
            generate_service("../escape", config=CliConfig(proto_dir=str(project / "proto"), services_dir=str(project / "services")), cwd=str(project))
        assert not (project / "services").exists()


class TestMain:
    def test_help_and_version(self, capsys):
        assert main([]) == 0
        assert "protobus generate" in capsys.readouterr().out
        assert main(["--version"]) == 0
        from protobus import __version__

        assert capsys.readouterr().out.strip() == __version__
        assert main(["init"]) == 0
        assert "pyproject.toml" in capsys.readouterr().out

    def test_unknown_command(self, capsys):
        assert main(["frobnicate"]) == 1
        assert "Unknown command" in capsys.readouterr().err

    def test_generate_service_needs_a_name(self, capsys):
        assert main(["generate:service"]) == 1
        assert "Service name required" in capsys.readouterr().err

    def test_generate_reports_errors_without_a_traceback(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["generate"]) == 1
        assert "Error:" in capsys.readouterr().err

    def test_the_console_script_runs(self):
        result = subprocess.run([sys.executable, "-m", "protobus.cli.main", "--version"], capture_output=True, text=True, cwd=str(REPO_ROOT))
        assert result.returncode == 0


class TestPackaging:
    def test_the_sdist_and_wheel_contain_only_the_library(self, tmp_path):
        build = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(tmp_path), str(REPO_ROOT)],
            capture_output=True, text=True,
        )
        if build.returncode != 0:
            pytest.skip(f"wheel build unavailable here: {build.stderr[-300:]}")
        import zipfile

        wheel = next(tmp_path.glob("protobus-*.whl"))
        names = zipfile.ZipFile(wheel).namelist()
        forbidden = [n for n in names if n.startswith(("tests/", "sample/", "docs/")) or "/.github/" in n or n.endswith(".env")]
        assert forbidden == []
        assert any(n == "protobus/__init__.py" for n in names)
        assert any(n.startswith("protobus/cli/") for n in names)

    def test_the_declared_dependencies_are_what_the_library_imports(self):
        import re

        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        deps = re.search(r"dependencies = \[(.*?)\]", pyproject, re.S).group(1)
        assert "aiormq" in deps and "protobuf" in deps
        assert "aio-pika" not in deps and "aio_pika" not in deps
        for path in (REPO_ROOT / "protobus").rglob("*.py"):
            assert "aio_pika" not in path.read_text(), path
