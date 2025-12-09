"""Integration tests for Protobus with RabbitMQ."""

import asyncio
import pytest

from protobus import (
    Context,
    MessageService,
    ServiceProxy,
    ServiceCluster,
    HandledError,
)


RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


class EchoService(MessageService):
    """Simple echo service for testing."""

    @property
    def service_name(self) -> str:
        return "test.EchoService"

    @property
    def proto_file_name(self) -> str:
        return "echo.proto"

    @property
    def Proto(self) -> str:
        # Return empty proto for testing
        return ""

    async def echo(self, data: dict, actor: str, correlation_id: str) -> dict:
        """Echo back the received message."""
        return {"echoed": data.get("message", ""), "actor": actor}

    async def add(self, data: dict, actor: str, correlation_id: str) -> dict:
        """Add two numbers."""
        a = data.get("a", 0)
        b = data.get("b", 0)
        return {"result": a + b}

    async def fail(self, data: dict, actor: str, correlation_id: str) -> dict:
        """Always fails with an error."""
        raise HandledError("This is an expected failure", code="TEST_ERROR")


@pytest.fixture
async def context():
    """Create and initialize a context."""
    ctx = Context()
    await ctx.init(RABBITMQ_URL)
    yield ctx
    await ctx.close()


@pytest.fixture
async def echo_service(context):
    """Create and initialize the echo service."""
    service = EchoService(context)
    # Parse empty proto
    context.factory.parse("", service.service_name)
    await service.init()
    yield service


@pytest.fixture
async def service_proxy(context, echo_service):
    """Create a service proxy for the echo service."""
    proxy = ServiceProxy(context, "test.EchoService")
    await proxy.init()
    yield proxy


class TestConnection:
    """Test connection functionality."""

    async def test_connect_disconnect(self):
        """Test basic connect and disconnect."""
        ctx = Context()
        await ctx.init(RABBITMQ_URL)
        assert ctx.is_connected
        await ctx.close()
        assert not ctx.is_connected


class TestMessageService:
    """Test MessageService functionality."""

    async def test_service_init(self, context):
        """Test service initialization."""
        service = EchoService(context)
        context.factory.parse("", service.service_name)
        await service.init()
        assert service.service_name == "test.EchoService"


class TestServiceProxy:
    """Test ServiceProxy RPC calls."""

    async def test_echo(self, service_proxy):
        """Test echo RPC call."""
        result = await service_proxy.echo({"message": "Hello, World!"}, "test-actor")
        assert result["echoed"] == "Hello, World!"
        assert result["actor"] == "test-actor"

    async def test_add(self, service_proxy):
        """Test add RPC call."""
        result = await service_proxy.add({"a": 5, "b": 3}, "test-actor")
        assert result["result"] == 8

    async def test_handled_error(self, service_proxy):
        """Test that handled errors are properly propagated."""
        with pytest.raises(Exception) as exc_info:
            await service_proxy.fail({}, "test-actor")
        assert "expected failure" in str(exc_info.value)


class TestEvents:
    """Test event pub/sub functionality."""

    async def test_publish_subscribe_event(self, context, echo_service):
        """Test event publishing and subscription."""
        received_events = []

        async def handler(data, topic):
            received_events.append({"data": data, "topic": topic})

        await echo_service.subscribe_event("test.event", handler)

        # Give time for subscription to be set up
        await asyncio.sleep(0.5)

        # Publish event
        await echo_service.publish_event("test.event", {"message": "test event"})

        # Wait for event to be received
        await asyncio.sleep(0.5)

        assert len(received_events) == 1
        assert received_events[0]["data"]["message"] == "test event"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
