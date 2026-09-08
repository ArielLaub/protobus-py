"""Heartbeat configuration, URL redaction, and the property mapping."""

from typing import List

import pytest
from pamqp.commands import Basic

import protobus.connection as connection_module
from protobus import Config, Connection, LogLevel, apply_heartbeat, get_log_level, redact_url, set_log_level, set_logger
from protobus.connection import build_properties, carried_properties, sanitize_headers
from protobus.logger import DefaultLogger

from ..helpers import FakeHandle


class TestHeartbeatConfiguration:
    def test_defaults_to_an_interval_that_detects_a_dead_peer_in_tens_of_seconds(self):
        assert Config.heartbeat_seconds() == 30

    def test_is_settable_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("AMQP_HEARTBEAT_SECONDS", "5")
        assert Config.heartbeat_seconds() == 5

    def test_applies_the_configured_interval_to_a_url_that_does_not_set_one(self):
        assert apply_heartbeat("amqp://guest:guest@localhost:5672/") == "amqp://guest:guest@localhost:5672/?heartbeat=30"

    def test_leaves_an_explicit_heartbeat_alone_including_zero(self, monkeypatch):
        monkeypatch.setenv("AMQP_HEARTBEAT_SECONDS", "30")
        assert apply_heartbeat("amqp://h:5672/vh?heartbeat=7") == "amqp://h:5672/vh?heartbeat=7"
        assert apply_heartbeat("amqp://h:5672/vh?heartbeat=0") == "amqp://h:5672/vh?heartbeat=0"

    def test_preserves_a_percent_encoded_vhost_and_any_other_parameters(self, monkeypatch):
        monkeypatch.setenv("AMQP_HEARTBEAT_SECONDS", "15")
        assert apply_heartbeat("amqp://u:p%40ss@host:5672/%2f") == "amqp://u:p%40ss@host:5672/%2f?heartbeat=15"
        assert apply_heartbeat("amqps://u:p@host/vh?frameMax=8192") == "amqps://u:p@host/vh?frameMax=8192&heartbeat=15"

    def test_hands_an_unparseable_url_straight_through(self):
        assert apply_heartbeat("not a url") == "not a url"


class TestRedactUrl:
    def test_replaces_the_password_but_keeps_the_rest_of_the_url_useful(self):
        assert redact_url("amqp://user:s3cret@rabbit:5672/vhost") == "amqp://user:***@rabbit:5672/vhost"

    def test_preserves_an_encoded_vhost(self):
        assert redact_url("amqp://user:s3cret@rabbit:5672/%2f") == "amqp://user:***@rabbit:5672/%2f"

    def test_leaves_a_credential_free_url_alone(self):
        assert redact_url("amqp://localhost") == "amqp://localhost"

    def test_redacts_anything_that_does_not_parse_as_a_url(self):
        assert redact_url("not a url") == "<redacted>"

    async def test_the_connection_never_logs_the_password(self, monkeypatch):
        lines: List[str] = []

        class Capturing:
            def info(self, m):
                lines.append(str(m))

            warn = debug = error = info

        set_logger(Capturing())
        level = get_log_level()
        set_log_level(LogLevel.Info)
        dialled = {}

        async def connect(url, *a, **kw):
            dialled["url"] = url
            return FakeHandle()

        monkeypatch.setattr(connection_module.aiormq, "connect", connect)
        try:
            conn = Connection()
            await conn.connect("amqp://user:s3cret@rabbit:5672/vhost")
            await conn.disconnect()
        finally:
            set_logger(DefaultLogger())
            set_log_level(level)
        assert "s3cret" not in "\n".join(lines)
        assert any("amqp://user:***@rabbit:5672/vhost" in line for line in lines)
        # The redaction is for the log only: the real credentials are dialled.
        assert dialled["url"].startswith("amqp://user:s3cret@rabbit:5672/vhost")


class TestProperties:
    def test_maps_a_properties_dict_onto_amqp_properties(self):
        props = build_properties({
            "content_type": "application/octet-stream",
            "correlation_id": "c", "reply_to": "r", "message_id": "m",
            "headers": {"a": 1}, "priority": 3, "persistent": True,
            "expiration": 250, "mandatory": True,
        })
        assert isinstance(props, Basic.Properties)
        assert props.content_type == "application/octet-stream"
        assert props.correlation_id == "c"
        assert props.reply_to == "r"
        assert props.message_id == "m"
        assert props.headers == {"a": 1}
        assert props.priority == 3
        assert props.delivery_mode == 2
        assert props.expiration == "250"

    def test_accepts_the_typescript_spellings(self):
        props = build_properties({"contentType": "x", "correlationId": "c", "replyTo": "r", "messageId": "m", "deliveryMode": 2, "appId": "a", "type": "t"})
        assert (props.content_type, props.correlation_id, props.reply_to, props.message_id, props.delivery_mode, props.app_id, props.message_type) == ("x", "c", "r", "m", 2, "a", "t")

    def test_none_values_are_left_absent(self):
        props = build_properties({"reply_to": None, "priority": None})
        assert props.reply_to is None
        assert props.priority is None

    def test_headers_are_sanitized_for_the_field_table(self):
        class Weird:
            def __str__(self):
                return "weird"

        assert sanitize_headers({"a": 1, "b": None, "c": Weird(), "d": [1, 2], "e": True}) == {"a": 1, "c": "weird", "d": [1, 2], "e": True}

    def test_carried_properties_copies_only_the_carried_set(self):
        original = Basic.Properties(
            content_type="ct", content_encoding="ce", priority=4, message_type="t", app_id="app",
            user_id="guest", expiration="100", delivery_mode=1, correlation_id="c", reply_to="r",
        )
        carried = carried_properties(original)
        assert carried == {"content_type": "ct", "content_encoding": "ce", "priority": 4, "message_type": "t", "app_id": "app"}

    def test_carried_properties_leaves_absent_absent(self):
        assert carried_properties(Basic.Properties()) == {}
