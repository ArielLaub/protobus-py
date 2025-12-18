"""Tests for CLI module."""

import os
import tempfile
from pathlib import Path

import pytest

from protobus.cli.config import CliConfig, load_config, find_proto_files
from protobus.cli.generate_service import generate_service, _to_snake_case, _extract_methods


class TestCliConfig:
    """Tests for CLI configuration."""

    def test_default_config(self):
        """Test default configuration values."""
        config = CliConfig()
        assert config.proto_dir == "./proto"
        assert config.types_output == "./types/proto.py"
        assert config.services_dir == "./services"

    def test_find_proto_files_empty(self):
        """Test finding proto files in non-existent directory."""
        files = find_proto_files("/nonexistent/path")
        assert files == []

    def test_find_proto_files(self):
        """Test finding proto files in a directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some proto files
            proto_dir = Path(tmpdir) / "proto"
            proto_dir.mkdir()
            (proto_dir / "test.proto").write_text("syntax = 'proto3';")
            (proto_dir / "other.proto").write_text("syntax = 'proto3';")

            files = find_proto_files(str(proto_dir))
            assert len(files) == 2
            assert any("test.proto" in f for f in files)
            assert any("other.proto" in f for f in files)


class TestGenerateService:
    """Tests for service generation."""

    def test_to_snake_case(self):
        """Test CamelCase to snake_case conversion."""
        assert _to_snake_case("MathService") == "math_service"
        assert _to_snake_case("HTTPClient") == "http_client"
        assert _to_snake_case("MyAPIHandler") == "my_api_handler"
        assert _to_snake_case("simple") == "simple"

    def test_extract_methods_no_file(self):
        """Test extracting methods from non-existent proto file."""
        import tempfile
        # Use a path that definitely doesn't exist
        with tempfile.TemporaryDirectory() as tmpdir:
            nonexistent = Path(tmpdir) / "nonexistent.proto"
            methods = _extract_methods(nonexistent, "TestService")
            assert methods == []

    def test_extract_methods(self):
        """Test extracting methods from proto file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            proto_file = Path(tmpdir) / "test.proto"
            proto_file.write_text("""
syntax = "proto3";
package test;

service MathService {
  rpc Add(AddRequest) returns (AddResponse);
  rpc Multiply(MultiplyRequest) returns (MultiplyResponse);
}

message AddRequest {
  int32 a = 1;
  int32 b = 2;
}
""")

            methods = _extract_methods(proto_file, "MathService")
            assert len(methods) == 2
            assert methods[0]["name"] == "Add"
            assert methods[0]["request"] == "AddRequest"
            assert methods[0]["response"] == "AddResponse"
            assert methods[1]["name"] == "Multiply"

    def test_generate_service_invalid_name(self):
        """Test generating service with invalid name format."""
        success = generate_service("InvalidName")
        assert success is False

    def test_generate_service(self):
        """Test generating a service stub."""
        with tempfile.TemporaryDirectory() as tmpdir:
            success = generate_service(
                "calculator.MathService",
                output_dir=tmpdir,
            )
            assert success is True

            # Check that the file was created
            service_file = Path(tmpdir) / "math_service_service.py"
            assert service_file.exists()

            # Check file contents
            content = service_file.read_text()
            assert "class MathServiceService(RunnableService)" in content
            assert 'return "calculator.MathService"' in content

    def test_generate_service_no_overwrite(self):
        """Test that existing service files are not overwritten."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # First generation
            success1 = generate_service(
                "calculator.MathService",
                output_dir=tmpdir,
            )
            assert success1 is True

            # Second generation should skip
            success2 = generate_service(
                "calculator.MathService",
                output_dir=tmpdir,
            )
            assert success2 is False


class TestCliMain:
    """Tests for main CLI entry point."""

    def test_init_command(self):
        """Test init command returns success."""
        from protobus.cli.main import main

        result = main(["init"])
        assert result == 0

    def test_no_command(self):
        """Test no command shows help and returns error."""
        from protobus.cli.main import main

        result = main([])
        assert result == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
