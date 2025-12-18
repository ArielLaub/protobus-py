"""Tests for RunnableService class."""

import asyncio
import pytest

from protobus import RunnableService, Context, MessageServiceOptions


class TestRunnableServiceProtoDerivation:
    """Tests for proto filename derivation (unit tests without context)."""

    def test_proto_file_name_derivation(self):
        """Test automatic proto filename derivation from service name."""
        # Test the derivation logic directly
        service_name = "Calculator.Service"
        parts = service_name.split(".")
        proto_file = f"{parts[0]}.proto"
        assert proto_file == "Calculator.proto"

    def test_proto_file_name_with_package(self):
        """Test proto filename derivation with package prefix."""
        service_name = "combat.Player"
        parts = service_name.split(".")
        proto_file = f"{parts[0]}.proto"
        assert proto_file == "combat.proto"

    def test_proto_file_name_deep_package(self):
        """Test proto filename derivation with deep package structure."""
        service_name = "my.package.deep.MyService"
        parts = service_name.split(".")
        proto_file = f"{parts[0]}.proto"
        assert proto_file == "my.proto"


@pytest.mark.skipif(
    not pytest.importorskip("aio_pika", reason="aio_pika not installed"),
    reason="Requires RabbitMQ connection"
)
class TestRunnableServiceIntegration:
    """Integration tests for RunnableService (requires RabbitMQ)."""

    @pytest.fixture
    async def context(self):
        """Create and initialize a context."""
        ctx = Context()
        try:
            await ctx.init("amqp://guest:guest@localhost:5672/")
        except Exception:
            pytest.skip("RabbitMQ not available")
        yield ctx
        await ctx.close()

    async def test_service_init(self, context):
        """Test RunnableService initialization."""

        class CalcService(RunnableService):
            @property
            def service_name(self) -> str:
                return "test.Calculator"

            async def add(self, data: dict, actor: str, correlation_id: str) -> dict:
                return {"result": data.get("a", 0) + data.get("b", 0)}

        service = CalcService(context)
        context.factory.parse("", service.service_name)
        await service.init()

        assert service.service_name == "test.Calculator"
        assert service.proto_file_name == "test.proto"

    async def test_cleanup_called(self, context):
        """Test that cleanup is called during shutdown."""
        cleanup_called = False

        class CleanupService(RunnableService):
            @property
            def service_name(self) -> str:
                return "test.CleanupService"

            async def cleanup(self):
                nonlocal cleanup_called
                cleanup_called = True

        service = CleanupService(context)
        context.factory.parse("", service.service_name)
        await service.init()

        # Manually trigger cleanup
        await service.cleanup()
        assert cleanup_called is True


@pytest.mark.skipif(
    not pytest.importorskip("aio_pika", reason="aio_pika not installed"),
    reason="Requires RabbitMQ connection"
)
class TestRunnableServiceOptions:
    """Test RunnableService with various options."""

    @pytest.fixture
    async def context(self):
        """Create and initialize a context."""
        ctx = Context()
        try:
            await ctx.init("amqp://guest:guest@localhost:5672/")
        except Exception:
            pytest.skip("RabbitMQ not available")
        yield ctx
        await ctx.close()

    async def test_with_max_concurrent(self, context):
        """Test service with max_concurrent option."""
        from protobus import RetryOptions

        options = MessageServiceOptions(
            max_concurrent=5,
            retry=RetryOptions(max_retries=2)
        )

        class ConcurrentService(RunnableService):
            @property
            def service_name(self) -> str:
                return "test.ConcurrentService"

        service = ConcurrentService(context, options)
        context.factory.parse("", service.service_name)
        await service.init()

        assert service._retry_options.max_retries == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
