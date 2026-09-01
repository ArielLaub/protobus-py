"""
The findings that only a real broker can settle.

Routing, publisher confirms, basic.return and heartbeat negotiation are
properties of RabbitMQ and the AMQP client, not of this library's control
flow, and a mock asserting what we already believe proves nothing about any
of them.

    docker-compose up -d
    PROTOBUS_TEST_AMQP_URL=amqp://guest:guest@localhost:5672/ \
        pytest tests/test_audit_broker.py -v

Every test declares its own topology under a unique prefix and tears it down,
so the file is safe to run repeatedly against a shared broker.
"""

import asyncio
import uuid

import aio_pika
import pytest
from aio_pika import ExchangeType, Message

from protobus.config import Config
from protobus.connection import (
    Connection,
    RetryOptions,
    _is_unroutable,
    _with_heartbeat,
)
from protobus.errors import UnroutableError

from .broker_url import broker_url


@pytest.fixture
async def connection():
    conn = Connection()
    try:
        await conn.connect(broker_url())
    except Exception:
        pytest.skip("RabbitMQ not available")
    yield conn
    await conn.close()


@pytest.fixture
def prefix():
    return f"audit-{uuid.uuid4().hex[:8]}"


class TestUnroutablePublishIsSurfaced:
    """
    aio-pika sends `mandatory` by default but reports the broker's return in
    the RETURN VALUE, not by raising. Nothing here ever looked, so a publish to
    a routing key with no binding behind it was dropped in silence — which is
    exactly what makes a service that is scaled to zero look like a timeout
    rather than an error.
    """

    async def test_a_publish_with_no_binding_raises(self, connection, prefix):
        channel = await connection.open_channel()
        exchange = await connection.ensure_exchange(
            channel, f"{prefix}.bus", ExchangeType.TOPIC
        )
        try:
            with pytest.raises(UnroutableError):
                await connection.publish(
                    channel, exchange, f"REQUEST.{prefix}.Nobody.listening", b"hello"
                )
        finally:
            await exchange.delete()
            await channel.close()

    async def test_a_publish_with_a_binding_succeeds(self, connection, prefix):
        channel = await connection.open_channel()
        exchange = await connection.ensure_exchange(
            channel, f"{prefix}.bus", ExchangeType.TOPIC
        )
        queue = await connection.ensure_queue(channel, f"{prefix}.Svc")
        await connection.bind_queue(queue, exchange, f"REQUEST.{prefix}.Svc.*")
        try:
            await connection.publish(
                channel, exchange, f"REQUEST.{prefix}.Svc.doThing", b"hello"
            )
            await asyncio.sleep(0.3)
            incoming = await queue.get(no_ack=True, fail=False)
            assert incoming is not None and incoming.body == b"hello"
        finally:
            await queue.delete(if_unused=False, if_empty=False)
            await exchange.delete()
            await channel.close()

    async def test_an_event_with_no_subscribers_is_not_an_error(
        self, connection, prefix
    ):
        """Events stay non-mandatory: no subscribers is normal for an event."""
        channel = await connection.open_channel()
        exchange = await connection.ensure_exchange(
            channel, f"{prefix}.events", ExchangeType.TOPIC
        )
        try:
            await connection.publish(
                channel,
                exchange,
                f"EVENT.{prefix}.NobodyCares",
                b"hello",
                mandatory=False,
            )
        finally:
            await exchange.delete()
            await channel.close()


class TestTheRetryPublishReachesTheRetryQueue:
    """
    _retry_message published to the topic bus exchange with the key
    "<delivery routing key>.retry" — four segments against the three-segment
    binding a service declares. Nothing was bound for it, the broker returned
    it, the return was never inspected and the delivery was acked: every
    message that exhausted a handler was destroyed on its first retry.
    """

    async def _topology(self, connection, prefix):
        channel = await connection.open_channel()
        exchange = await connection.ensure_exchange(
            channel, f"{prefix}.bus", ExchangeType.TOPIC
        )
        service_queue = await connection.ensure_queue(channel, f"{prefix}.Svc")
        await connection.bind_queue(
            service_queue, exchange, f"REQUEST.{prefix}.Svc.*"
        )
        retry_queue = await connection.ensure_queue(
            channel,
            f"{prefix}.Svc.retry",
            arguments={
                "x-message-ttl": 60000,
                "x-dead-letter-exchange": f"{prefix}.bus",
                "x-dead-letter-routing-key": f"REQUEST.{prefix}.Svc.*",
            },
        )
        return channel, exchange, service_queue, retry_queue

    async def test_the_old_key_was_genuinely_unroutable(self, connection, prefix):
        """Pins the defect itself, so a regression cannot pass unnoticed."""
        channel, exchange, service_queue, retry_queue = await self._topology(
            connection, prefix
        )
        try:
            result = await exchange.publish(
                Message(body=b"retry-me"),
                routing_key=f"REQUEST.{prefix}.Svc.doThing.retry",
                mandatory=True,
            )
            assert _is_unroutable(result), (
                "the broker accepted the old retry key; this test no longer "
                "pins the defect"
            )
        finally:
            await retry_queue.delete(if_unused=False, if_empty=False)
            await service_queue.delete(if_unused=False, if_empty=False)
            await exchange.delete()
            await channel.close()

    async def test_a_retried_message_lands_in_the_retry_queue(
        self, connection, prefix
    ):
        channel, exchange, service_queue, retry_queue = await self._topology(
            connection, prefix
        )
        try:
            await connection.publish(
                channel, exchange, f"REQUEST.{prefix}.Svc.doThing", b"work"
            )
            await asyncio.sleep(0.3)
            delivery = await service_queue.get(no_ack=True, fail=False)
            assert delivery is not None

            await connection._retry_message(
                channel,
                delivery,
                retry_count=0,
                error=RuntimeError("handler blew up"),
                retry_opts=RetryOptions(max_retries=3, retry_delay_ms=60000),
                queue_name=f"{prefix}.Svc",
            )

            await asyncio.sleep(0.4)
            retried = await retry_queue.get(no_ack=True, fail=False)
            assert retried is not None, "the retry was silently dropped"
            assert retried.body == b"work"
            assert retried.headers.get("x-retry-count") == 1
        finally:
            await retry_queue.delete(if_unused=False, if_empty=False)
            await service_queue.delete(if_unused=False, if_empty=False)
            await exchange.delete()
            await channel.close()

    async def test_a_retry_with_no_queue_raises_rather_than_dropping(
        self, connection, prefix
    ):
        """The caller requeues on a failed handoff; it must be told."""
        channel = await connection.open_channel()
        exchange = await connection.ensure_exchange(
            channel, f"{prefix}.bus", ExchangeType.TOPIC
        )
        service_queue = await connection.ensure_queue(channel, f"{prefix}.Svc")
        await connection.bind_queue(
            service_queue, exchange, f"REQUEST.{prefix}.Svc.*"
        )
        try:
            await connection.publish(
                channel, exchange, f"REQUEST.{prefix}.Svc.doThing", b"work"
            )
            await asyncio.sleep(0.3)
            delivery = await service_queue.get(no_ack=True, fail=False)
            assert delivery is not None

            # No .retry queue was declared.
            with pytest.raises(UnroutableError):
                await connection._retry_message(
                    channel,
                    delivery,
                    retry_count=0,
                    error=RuntimeError("boom"),
                    retry_opts=RetryOptions(),
                    queue_name=f"{prefix}.Svc",
                )
        finally:
            await service_queue.delete(if_unused=False, if_empty=False)
            await exchange.delete()
            await channel.close()


class TestTheHeartbeatIsNegotiated:
    """
    Nothing set a heartbeat, so the interval was whatever RabbitMQ proposed and
    a peer that vanished without closing its socket went unnoticed for around
    two minutes while the connection reported itself healthy.
    """

    def test_a_heartbeat_is_added_to_a_plain_url(self):
        assert _with_heartbeat("amqp://guest:guest@localhost:5672/") == (
            "amqp://guest:guest@localhost:5672/?heartbeat=30"
        )

    def test_an_existing_query_is_preserved(self):
        assert _with_heartbeat("amqp://h:5672/vh?name=svc") == (
            "amqp://h:5672/vh?name=svc&heartbeat=30"
        )

    def test_an_explicit_heartbeat_is_left_alone(self):
        url = "amqp://h:5672/vh?heartbeat=5"
        assert _with_heartbeat(url) == url

    def test_heartbeat_zero_is_respected_as_a_deliberate_disable(self):
        url = "amqp://h:5672/vh?heartbeat=0"
        assert _with_heartbeat(url) == url

    def test_a_percent_encoded_vhost_is_not_re_encoded(self):
        """Re-encoding would connect to the wrong vhost, or fail outright."""
        assert _with_heartbeat("amqp://h:5672/%2Fprod").startswith(
            "amqp://h:5672/%2Fprod?"
        )

    async def test_the_broker_agrees_the_interval(self, connection):
        """The negotiated value comes back from the broker, so this is not a
        test of our own string handling."""
        transport = getattr(connection._connection, "transport", None)
        inner = getattr(transport, "connection", None)
        negotiated = getattr(inner, "heartbeat_timeout", None)
        assert negotiated == Config.amqp_heartbeat_seconds(), (
            f"broker negotiated {negotiated}, expected "
            f"{Config.amqp_heartbeat_seconds()}"
        )

    async def test_without_the_fix_the_broker_proposes_its_own(self):
        """Pins what the default actually was: RabbitMQ's 60s, giving ~2min
        worst-case detection of a peer that vanished."""
        raw = await aio_pika.connect_robust(broker_url())
        try:
            inner = getattr(getattr(raw, "transport", None), "connection", None)
            assert getattr(inner, "heartbeat_timeout", None) == 60
        finally:
            await raw.close()
