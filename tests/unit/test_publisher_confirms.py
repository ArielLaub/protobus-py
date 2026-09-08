"""
A resolved publish means a positive broker confirm; every other outcome is a
distinct, typed exception.
"""

import asyncio

import pytest

from protobus import (
    ChannelClosedError,
    Connection,
    PublishConfirmTimeoutError,
    PublishNackedError,
    UnroutableError,
)

from ..helpers import FakeChannel, tick


def track(coro):
    task = asyncio.ensure_future(coro)
    state = {"task": task}
    return state


class TestPublisherConfirms:
    async def test_does_not_resolve_until_the_broker_confirms(self):
        conn = Connection()
        ch = FakeChannel(auto_confirm=False)
        task = asyncio.ensure_future(conn.publish(ch, "ex", "rk", b"x", {}))
        await tick(3)
        # The bytes went out, but RabbitMQ has said nothing.
        assert len(ch.published) == 1
        assert not task.done()

        ch.published[0].confirm()
        await task

    async def test_raises_publish_nacked_error_when_the_broker_nacks(self):
        conn = Connection()
        ch = FakeChannel(auto_confirm=False)
        task = asyncio.ensure_future(conn.publish(ch, "ex", "rk", b"x", {}))
        await tick(3)
        ch.published[0].nack()
        with pytest.raises(PublishNackedError):
            await task

    async def test_raises_unroutable_error_when_a_mandatory_message_is_returned(self):
        conn = Connection()
        ch = FakeChannel(auto_confirm=False)
        task = asyncio.ensure_future(conn.publish(ch, "ex", "rk", b"x", {"mandatory": True}))
        await tick(3)
        assert ch.published[0].mandatory is True
        ch.published[0].return_unroutable()
        with pytest.raises(UnroutableError) as info:
            await task
        assert info.value.message_id == ch.published[0].properties.message_id

    async def test_stamps_a_message_id_so_returns_can_be_correlated(self):
        conn = Connection()
        ch = FakeChannel()
        await conn.publish(ch, "ex", "rk", b"x", {"mandatory": True})
        assert isinstance(ch.published[0].properties.message_id, str)
        assert ch.published[0].properties.message_id

    async def test_preserves_a_caller_supplied_message_id(self):
        conn = Connection()
        ch = FakeChannel()
        await conn.publish(ch, "ex", "rk", b"x", {"message_id": "stable-id"})
        assert ch.published[0].properties.message_id == "stable-id"

    async def test_mints_a_distinct_id_per_publish_when_none_is_given(self):
        conn = Connection()
        ch = FakeChannel()
        await conn.publish(ch, "ex", "rk", b"a", {})
        await conn.publish(ch, "ex", "rk", b"b", {})
        ids = [p.properties.message_id for p in ch.published]
        assert all(isinstance(i, str) and i for i in ids)
        assert ids[0] != ids[1]

    async def test_raises_publish_confirm_timeout_error_when_no_confirm_arrives(self, monkeypatch):
        monkeypatch.setenv("PUBLISH_CONFIRM_TIMEOUT_MS", "40")
        conn = Connection()
        ch = FakeChannel(auto_confirm=False)
        with pytest.raises(PublishConfirmTimeoutError) as info:
            await conn.publish(ch, "ex", "rk", b"x", {})
        assert info.value.message_id

    async def test_raises_channel_closed_error_when_the_channel_closes(self):
        conn = Connection()
        ch = FakeChannel(auto_confirm=False)
        task = asyncio.ensure_future(conn.publish(ch, "ex", "rk", b"x", {}))
        await tick(3)
        ch.emit_close()
        with pytest.raises(ChannelClosedError):
            await task

    async def test_a_channel_closing_underneath_aiormq_reads_as_channel_closed(self):
        conn = Connection()
        ch = FakeChannel(auto_confirm=False)
        task = asyncio.ensure_future(conn.publish(ch, "ex", "rk", b"x", {}))
        await tick(3)
        # aiormq cancels the confirmation future when its channel goes away.
        ch.published[0].future.cancel()
        with pytest.raises(ChannelClosedError):
            await task

    async def test_bounds_the_number_of_unconfirmed_publishes_in_flight(self, monkeypatch):
        monkeypatch.setenv("MAX_OUTSTANDING_CONFIRMS", "2")
        conn = Connection()
        ch = FakeChannel(auto_confirm=False)
        tasks = [asyncio.ensure_future(conn.publish(ch, "ex", "rk", body, {})) for body in (b"a", b"b", b"c")]
        await tick(3)
        # The third must not even reach the channel until one slot frees.
        assert len(ch.published) == 2
        assert not tasks[2].done()

        ch.published[0].confirm()
        await tick(3)
        assert len(ch.published) == 3

        ch.published[1].confirm()
        ch.published[2].confirm()
        await asyncio.gather(*tasks)

    async def test_publish_to_queue_uses_the_default_exchange(self):
        conn = Connection()
        ch = FakeChannel()
        await conn.publish_to_queue(ch, "Svc.DLQ", b"x", {"persistent": True})
        assert ch.published[0].exchange == ""
        assert ch.published[0].routing_key == "Svc.DLQ"
        assert ch.published[0].properties.delivery_mode == 2

    async def test_cancelling_the_caller_cancels_the_publish(self):
        conn = Connection()
        ch = FakeChannel(auto_confirm=False)
        task = asyncio.ensure_future(conn.publish(ch, "ex", "rk", b"x", {}))
        await tick(3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ch.published[0].future.cancelled()
