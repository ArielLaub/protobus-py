"""
Test doubles for the AMQP boundary.

``FakeChannel`` records what the connection layer does to it — acks,
rejects, publishes — and lets a test drive deliveries and confirms by hand,
so the consume/settle state machine and the publish contract can be
exercised without a broker. ``FakeConnection`` stands in for a Connection
from a listener's or dispatcher's point of view.
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from aiormq.abc import DeliveredMessage
from pamqp.commands import Basic
from pamqp.header import ContentHeader

from protobus.connection import ConsumeOptions
from protobus.events import EventEmitter


def make_delivery(
    body: bytes = b"payload",
    routing_key: str = "REQUEST.Svc.Api.doThing",
    correlation_id: Optional[str] = "cid-1",
    reply_to: Optional[str] = "callback.queue",
    headers: Optional[Dict[str, Any]] = None,
    message_id: Optional[str] = "mid-1",
    redelivered: bool = False,
    delivery_tag: int = 1,
    channel: Any = None,
    **properties: Any,
) -> DeliveredMessage:
    """An aiormq DeliveredMessage as a consumer would receive it."""
    props = Basic.Properties(
        correlation_id=correlation_id,
        reply_to=reply_to,
        headers=headers if headers is not None else {},
        message_id=message_id,
        **properties,
    )
    return DeliveredMessage(
        delivery=Basic.Deliver(
            consumer_tag="tag", delivery_tag=delivery_tag, redelivered=redelivered,
            exchange="proto.bus", routing_key=routing_key,
        ),
        header=ContentHeader(properties=props, body_size=len(body)),
        body=body,
        channel=channel,
    )


class PublishRecord:
    def __init__(self, exchange: str, routing_key: str, content: bytes, properties: Basic.Properties, mandatory: bool) -> None:
        self.exchange = exchange
        self.routing_key = routing_key
        self.content = content
        self.properties = properties
        self.mandatory = mandatory
        self.future: "asyncio.Future[Any]" = asyncio.get_event_loop().create_future()

    @property
    def headers(self) -> Dict[str, Any]:
        return dict(self.properties.headers or {})

    def confirm(self) -> None:
        if not self.future.done():
            self.future.set_result(Basic.Ack(delivery_tag=1))

    def nack(self) -> None:
        import aiormq

        if not self.future.done():
            self.future.set_exception(aiormq.exceptions.DeliveryError(None, Basic.Nack(delivery_tag=1)))

    def return_unroutable(self) -> None:
        if not self.future.done():
            returned = DeliveredMessage(
                delivery=Basic.Return(reply_code=312, reply_text="NO_ROUTE", exchange=self.exchange, routing_key=self.routing_key),
                header=ContentHeader(properties=self.properties, body_size=len(self.content)),
                body=self.content,
                channel=None,
            )
            self.future.set_result(returned)


class FakeChannel:
    """An aiormq-shaped channel that records everything."""

    def __init__(self, auto_confirm: bool = True) -> None:
        self.auto_confirm = auto_confirm
        self.acked: List[DeliveredMessage] = []
        self.rejected: List[Dict[str, Any]] = []
        self.published: List[PublishRecord] = []
        self.consumers: Dict[str, Callable[[DeliveredMessage], Awaitable[None]]] = {}
        self.prefetch: Optional[int] = None
        self.declared_exchanges: List[Dict[str, Any]] = []
        self.declared_queues: List[Dict[str, Any]] = []
        self.bindings: List[Dict[str, Any]] = []
        self.cancelled: List[str] = []
        self.closing: "asyncio.Future[Any]" = asyncio.get_event_loop().create_future()
        self.closed = False
        self._consume_count = 0

    # -- what the connection layer calls ------------------------------------

    async def basic_qos(self, prefetch_count: int = 0, **_kw: Any) -> None:
        self.prefetch = prefetch_count

    async def basic_consume(self, queue: str, callback: Any, no_ack: bool = False, exclusive: bool = False, consumer_tag: Optional[str] = None, arguments: Any = None, **_kw: Any) -> Any:
        self._consume_count += 1
        tag = consumer_tag or f"tag-{self._consume_count}"
        self.consumers[tag] = callback
        self.last_consume = {"queue": queue, "no_ack": no_ack, "exclusive": exclusive, "consumer_tag": tag}
        return Basic.ConsumeOk(consumer_tag=tag)

    async def basic_ack(self, delivery_tag: int, multiple: bool = False, **_kw: Any) -> None:
        self.acked.append(delivery_tag)

    async def basic_reject(self, delivery_tag: int, requeue: bool = True, **_kw: Any) -> None:
        self.rejected.append({"delivery_tag": delivery_tag, "requeue": requeue})

    async def basic_cancel(self, consumer_tag: str, **_kw: Any) -> None:
        self.cancelled.append(consumer_tag)
        self.consumers.pop(consumer_tag, None)

    def basic_publish(self, body: bytes, *, exchange: str = "", routing_key: str = "", properties: Any = None, mandatory: bool = False, **_kw: Any) -> "asyncio.Future[Any]":
        record = PublishRecord(exchange, routing_key, body, properties or Basic.Properties(), mandatory)
        self.published.append(record)
        if self.auto_confirm:
            asyncio.get_event_loop().call_soon(record.confirm)
        return record.future

    async def exchange_declare(self, exchange: str = "", exchange_type: str = "direct", **kw: Any) -> None:
        self.declared_exchanges.append({"exchange": exchange, "type": exchange_type, **kw})

    async def queue_declare(self, queue: str = "", **kw: Any) -> Any:
        name = queue or f"amq.gen-{len(self.declared_queues) + 1}"
        self.declared_queues.append({"queue": name, **kw})
        return type("DeclareOk", (), {"queue": name})()

    async def queue_bind(self, queue: str, exchange: str, routing_key: str = "", arguments: Any = None, **_kw: Any) -> None:
        self.bindings.append({"queue": queue, "exchange": exchange, "routing_key": routing_key})

    async def queue_unbind(self, **kw: Any) -> None:
        pass

    async def queue_delete(self, **kw: Any) -> None:
        pass

    async def queue_purge(self, **kw: Any) -> None:
        pass

    async def close(self, *_a: Any, **_kw: Any) -> None:
        self.closed = True
        if not self.closing.done():
            self.closing.set_result(None)

    # -- test controls --------------------------------------------------------

    async def deliver(self, message: DeliveredMessage, tag: Optional[str] = None) -> None:
        """Deliver to the (first) consumer, awaiting the whole settle."""
        if not self.consumers:
            raise RuntimeError("no consumer")
        callback = self.consumers[tag] if tag else next(iter(self.consumers.values()))
        await callback(message)

    def emit_close(self) -> None:
        if not self.closing.done():
            self.closing.set_result(None)

    def published_to(self, exchange: str) -> List[PublishRecord]:
        return [p for p in self.published if p.exchange == exchange]

    def sent_to_queue(self, queue: str) -> List[PublishRecord]:
        return [p for p in self.published if p.exchange == "" and p.routing_key == queue]


class FakeHandle:
    """An aiormq connection handle."""

    def __init__(self, channel_factory: Optional[Callable[[], FakeChannel]] = None) -> None:
        self.closing: "asyncio.Future[Any]" = asyncio.get_event_loop().create_future()
        self.channels: List[FakeChannel] = []
        self._factory = channel_factory or FakeChannel
        self.closed = False

    async def channel(self, **_kw: Any) -> FakeChannel:
        ch = self._factory()
        self.channels.append(ch)
        return ch

    async def close(self, *_a: Any, **_kw: Any) -> None:
        self.closed = True
        if not self.closing.done():
            self.closing.set_result(None)

    def drop(self, exc: Optional[BaseException] = None) -> None:
        """The socket died."""
        if not self.closing.done():
            if exc is not None:
                self.closing.set_exception(exc)
            else:
                self.closing.set_result(None)


class FakeConnection(EventEmitter):
    """
    A stand-in Connection for listener and dispatcher tests. Every AMQP
    operation is recorded; ``publish`` resolves at once unless a test
    installs ``publish_hook``.
    """

    def __init__(self, coordinated: bool = True) -> None:
        super().__init__()
        self.is_connected = True
        self.is_reconnecting = False
        self.is_ready = True
        self.channels: List[FakeChannel] = []
        self.declared_exchanges: List[Dict[str, Any]] = []
        self.declared_queues: List[Dict[str, Any]] = []
        self.bindings: List[Dict[str, Any]] = []
        self.consumes: List[Dict[str, Any]] = []
        self.publishes: List[Dict[str, Any]] = []
        self.cancels: List[str] = []
        self.prefetches: List[int] = []
        self.restorers: List[Callable[..., Awaitable[None]]] = []
        self.publish_hook: Optional[Callable[..., Awaitable[Any]]] = None
        self.cancelled_streams: List[str] = []
        self.in_flight_deliveries = 0
        # Every topology operation in order, for tests that care about order.
        self.operations: List[Tuple[str, Any]] = []
        if not coordinated:
            # An IConnection predating register_restorer/when_ready: the
            # library checks for a callable, so None reads as absent.
            self.register_restorer = None  # type: ignore[assignment]
            self.when_ready = None  # type: ignore[assignment]

    def register_restorer(self, restore: Callable[..., Awaitable[None]]) -> Callable[[], None]:
        self.restorers.append(restore)

        def detach() -> None:
            if restore in self.restorers:
                self.restorers.remove(restore)

        return detach

    async def when_ready(self, timeout_ms: Optional[int] = None) -> None:
        return None

    async def run_restorers(self, generation: int = 1) -> None:
        for restore in list(self.restorers):
            await restore(generation)

    def cancel_stream(self, correlation_id: str) -> bool:
        self.cancelled_streams.append(correlation_id)
        return True

    async def drain_in_flight(self, timeout_ms: int) -> bool:
        return True

    async def open_channel(self) -> FakeChannel:
        ch = FakeChannel()
        self.channels.append(ch)
        return ch

    async def close_channel(self, channel: FakeChannel) -> None:
        await channel.close()

    async def set_prefetch(self, channel: Any, count: int) -> None:
        self.prefetches.append(count)

    async def declare_exchange(self, channel: Any, exchange: str, exchange_type: str, options: Any = None) -> None:
        self.declared_exchanges.append({"exchange": exchange, "type": exchange_type, "options": options or {}})
        self.operations.append((f"declare_exchange:{exchange}", options))

    async def declare_queue(self, channel: Any, queue_name: str, options: Any = None) -> str:
        name = queue_name or f"amq.gen-{len(self.declared_queues) + 1}"
        self.declared_queues.append({"queue": name, "options": options or {}})
        self.operations.append((f"declare_queue:{name}", options))
        return name

    async def bind_queue(self, channel: Any, queue: str, exchange: str, routing_key: str, args: Any = None) -> None:
        self.bindings.append({"queue": queue, "exchange": exchange, "routing_key": routing_key})
        self.operations.append((f"bind:{queue}:{exchange}:{routing_key}", None))

    async def unbind_queue(self, *a: Any, **kw: Any) -> None:
        pass

    async def delete_queue(self, *a: Any, **kw: Any) -> None:
        pass

    async def consume(self, channel: Any, queue_name: str, message_handler: Any, options: ConsumeOptions, late_ack: bool, retry_options: Any = None, processing_timeout_ms: Any = None) -> str:
        self.operations.append(("consume", queue_name))
        self.consumes.append({
            "channel": channel, "queue": queue_name, "handler": message_handler, "options": options,
            "late_ack": late_ack, "retry_options": retry_options, "processing_timeout_ms": processing_timeout_ms,
        })
        return options.consumer_tag or "tag"

    async def cancel(self, channel: Any, consumer_tag: str) -> None:
        self.cancels.append(consumer_tag)

    async def publish(self, channel: Any, exchange_name: str, routing_key: str, content: bytes, properties: Any = None) -> None:
        self.publishes.append({"channel": channel, "exchange": exchange_name, "routing_key": routing_key, "content": content, "properties": properties or {}})
        if self.publish_hook is not None:
            await self.publish_hook(channel, exchange_name, routing_key, content, properties or {})

    async def publish_to_queue(self, channel: Any, queue_name: str, content: bytes, properties: Any = None) -> None:
        await self.publish(channel, "", queue_name, content, properties)

    async def connect(self, *a: Any, **kw: Any) -> Any:
        return None

    async def disconnect(self) -> None:
        self.is_connected = False

    close = disconnect

    def disconnect_now(self) -> None:
        """Simulate the socket dying."""
        self.is_connected = False
        self.is_ready = False
        self.emit("disconnected")

    async def reconnect_now(self) -> None:
        """Simulate the socket coming back and restoration completing."""
        self.is_connected = True
        self.is_reconnecting = True
        await self.run_restorers()
        self.is_reconnecting = False
        self.is_ready = True
        self.emit("reconnected")


def make_factory(proto: str, module_name: Optional[str] = None):
    from protobus import MessageFactory

    factory = MessageFactory()
    factory.init([])
    factory.parse(proto, module_name)
    return factory


class FakeContext:
    """Just enough IContext for a MessageService or ServiceProxy."""

    def __init__(self, factory: Any, connection: Optional[FakeConnection] = None) -> None:
        self.factory = factory
        self.connection = connection or FakeConnection()
        self.published: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.reply: Any = None

    @property
    def is_connected(self) -> bool:
        return self.connection.is_connected

    @property
    def is_reconnecting(self) -> bool:
        return self.connection.is_reconnecting

    async def publish_message(self, content: bytes, routing_key: str, rpc: bool = True, timeout_ms: Any = None, options: Any = None, priority: Any = None) -> Any:
        self.published.append({"content": content, "routing_key": routing_key, "rpc": rpc, "timeout_ms": timeout_ms, "options": options})
        if callable(self.reply):
            return await self.reply(content, routing_key)
        return self.reply

    def publish_streaming_message(self, content: bytes, routing_key: str, idle_timeout_ms: Any = None, options: Any = None) -> Any:
        self.published.append({"content": content, "routing_key": routing_key, "streaming": True, "options": options})
        return self.reply

    async def publish_event(self, event_type: str, content: Any, topic: Optional[str] = None) -> None:
        self.events.append({"type": event_type, "content": content, "topic": topic})


async def tick(n: int = 1) -> None:
    for _ in range(n):
        await asyncio.sleep(0)
