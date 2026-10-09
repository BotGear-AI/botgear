# unmanaged
"""ws_endpoint — GET /v1/ws, the runtime bus over WebSocket.

Two parts:
  1. The api_key auth adapter (_payload_from_api_key) on its own. The
     JWT path (_decode_token) has no comparable unit here because it's
     exercised directly by every protocol test below instead (minting
     a real token and connecting) — a payload shape test for it would
     just be a smaller, redundant version of that. _payload_from_api_key
     adapts an api_client secret into the SAME payload shape, used for
     peer-to-peer federation credentials (see test_peer_manager.py's
     Option B tests) as well as any other external integration that
     already holds an api_client key.
  2. The actual protocol (register_ws_routes' GET /v1/ws handler) end
     to end via TestClient.websocket_connect — see that section's own
     docstring below.
"""
from __future__ import annotations

import jwt as _pyjwt
import pytest
from auth import get_jwt_secret
from fastapi import FastAPI
from fastapi.testclient import TestClient

from botgear.runtime.ws_endpoint import _payload_from_api_key, register_ws_routes


def test_valid_key_produces_user_shaped_payload(monkeypatch):
    monkeypatch.setattr(
        "botgear.services.api_client_service.validate_api_client_secret",
        lambda secret: {
            "id": "abc123", "name": "peer:worke", "roles": ["Admin"],
            "auth_type": "shared_secret",
        },
    )
    payload = _payload_from_api_key("whatever-the-secret-was")
    assert payload is not None
    assert payload["sub"] == "abc123"
    assert payload["user"]["id"] == "abc123"
    assert payload["user"]["roles"] == ["Admin"]
    assert payload["user"]["email"] == "peer:worke@api-client"
    assert payload["user"]["status"] == "active"


def test_no_exp_claim():
    """api_client credentials don't expire — the payload must carry no
    'exp' key at all, matching the subprocess-token shape (not an exp
    far in the future, literally absent) so the WS reauth watchdog's
    'exp is None -> nothing to enforce' path applies."""
    import botgear.runtime.ws_endpoint as ws_mod
    import unittest.mock as mock
    with mock.patch(
        "botgear.services.api_client_service.validate_api_client_secret",
        return_value={"id": "x", "name": "n", "roles": []},
    ):
        payload = ws_mod._payload_from_api_key("key")
    assert "exp" not in payload


def test_invalid_key_returns_none(monkeypatch):
    monkeypatch.setattr(
        "botgear.services.api_client_service.validate_api_client_secret",
        lambda secret: None,
    )
    assert _payload_from_api_key("not-a-real-key") is None


def test_lookup_failure_is_swallowed_not_raised(monkeypatch):
    """A DB outage (validate_api_client_secret raises per its own
    documented contract) must not crash the WS handshake — treat it
    as an auth rejection, same as an invalid key."""
    def _boom(secret):
        raise RuntimeError("Database client not available")
    monkeypatch.setattr(
        "botgear.services.api_client_service.validate_api_client_secret", _boom,
    )
    assert _payload_from_api_key("key") is None


def test_missing_name_falls_back_to_id(monkeypatch):
    monkeypatch.setattr(
        "botgear.services.api_client_service.validate_api_client_secret",
        lambda secret: {"id": "xyz", "roles": []},
    )
    payload = _payload_from_api_key("key")
    assert payload["user"]["email"] == "xyz@api-client"


# ─── GET /v1/ws — the actual protocol, end to end via TestClient ───────
#
# fastapi.testclient.TestClient's websocket_connect drives the real ASGI
# route (handshake, the async message loop, task cleanup on disconnect)
# in-process — no real socket needed, and no need to mock anything below
# the auth boundary. Each test uses a topic name unique to it since the
# bus (get_bus()) is the real process-wide singleton, same convention as
# test_lifecycle.py / test_reconciler.py.

def _token(**claims) -> str:
    payload = {"sub": "u1", "user": {"id": "u1", "roles": ["Admin"]}, **claims}
    return _pyjwt.encode(payload, get_jwt_secret(), algorithm="HS256")


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_ws_routes(app)
    return TestClient(app)


def test_connect_without_any_credential_closes_4401(client):
    with client.websocket_connect(f"/v1/ws") as ws:
        with pytest.raises(Exception) as exc_info:
            ws.receive_text()
    assert "4401" in str(exc_info.value) or getattr(exc_info.value, "code", None) == 4401


def test_connect_with_a_bad_token_closes_4403(client):
    with client.websocket_connect("/v1/ws?token=not-a-real-jwt") as ws:
        with pytest.raises(Exception) as exc_info:
            ws.receive_text()
    assert "4403" in str(exc_info.value) or getattr(exc_info.value, "code", None) == 4403


def test_subscribe_ack_then_receive_a_published_message(client):
    topic = "/test/ws-endpoint/pubsub"
    with client.websocket_connect(f"/v1/ws?token={_token()}") as sub, \
         client.websocket_connect(f"/v1/ws?token={_token()}") as pub:
        sub.send_json({"id": "1", "method": "subscribe", "data": {"topic": topic}})
        ack = sub.receive_json()
        assert ack == {"method": "ack", "id": "1", "topic": topic, "subscribed": True}

        pub.send_json({"id": "2", "method": "publish", "data": {"topic": topic, "payload": {"x": 1}}})
        pub_ack = pub.receive_json()
        assert pub_ack["method"] == "ack" and pub_ack["delivered"] >= 1

        msg = sub.receive_json()
        assert msg["method"] == "message"
        assert msg["topic"] == topic
        assert msg["payload"] == {"x": 1}


def test_double_subscribe_is_idempotent(client):
    topic = "/test/ws-endpoint/idempotent"
    with client.websocket_connect(f"/v1/ws?token={_token()}") as ws:
        ws.send_json({"id": "1", "method": "subscribe", "data": {"topic": topic}})
        assert ws.receive_json()["subscribed"] is True
        ws.send_json({"id": "2", "method": "subscribe", "data": {"topic": topic}})
        assert ws.receive_json() == {"method": "ack", "id": "2", "topic": topic, "subscribed": True}


def test_unsubscribe_stops_delivery(client):
    topic = "/test/ws-endpoint/unsub"
    with client.websocket_connect(f"/v1/ws?token={_token()}") as sub, \
         client.websocket_connect(f"/v1/ws?token={_token()}") as pub:
        sub.send_json({"id": "1", "method": "subscribe", "data": {"topic": topic}})
        sub.receive_json()
        sub.send_json({"id": "2", "method": "unsubscribe", "data": {"topic": topic}})
        assert sub.receive_json() == {"method": "ack", "id": "2", "topic": topic, "subscribed": False}

        pub.send_json({"id": "3", "method": "publish", "data": {"topic": topic, "payload": "hi"}})
        pub_ack = pub.receive_json()
        assert pub_ack["delivered"] == 0  # no subscribers left


def test_request_carries_reply_to_through_to_the_delivered_message(client):
    topic = "/test/ws-endpoint/request"
    with client.websocket_connect(f"/v1/ws?token={_token()}") as sub, \
         client.websocket_connect(f"/v1/ws?token={_token()}") as requester:
        sub.send_json({"id": "1", "method": "subscribe", "data": {"topic": topic}})
        sub.receive_json()

        requester.send_json({
            "id": "2", "method": "request", "reply_to": "/test/ws-endpoint/reply",
            "data": {"topic": topic, "payload": "ping"},
        })
        requester.receive_json()

        msg = sub.receive_json()
        assert msg["reply_to"] == "/test/ws-endpoint/reply"
        assert msg["payload"] == "ping"


def test_list_topics_returns_topics_and_patterns(client):
    topic = "/test/ws-endpoint/list"
    with client.websocket_connect(f"/v1/ws?token={_token()}") as ws:
        ws.send_json({"id": "1", "method": "subscribe", "data": {"topic": topic}})
        ws.receive_json()
        ws.send_json({"id": "2", "method": "list_topics"})
        out = ws.receive_json()
        assert out["method"] == "topics"
        assert any(t["name"] == topic for t in out["topics"])


def test_malformed_json_frame_gets_an_error_not_a_disconnect(client):
    with client.websocket_connect(f"/v1/ws?token={_token()}") as ws:
        ws.send_text("{not valid json")
        assert ws.receive_json() == {"method": "error", "error": "invalid_json"}
        # Connection survives — a well-formed frame right after still works.
        ws.send_json({"id": "1", "method": "list_topics"})
        assert ws.receive_json()["method"] == "topics"


def test_unknown_method_returns_an_error_naming_it(client):
    with client.websocket_connect(f"/v1/ws?token={_token()}") as ws:
        ws.send_json({"id": "1", "method": "frobnicate"})
        assert ws.receive_json() == {
            "method": "error", "id": "1", "error": "unknown_method", "method_received": "frobnicate",
        }


@pytest.mark.parametrize("method", ["subscribe", "publish", "request"])
def test_missing_topic_is_rejected_for_every_topic_taking_method(client, method):
    with client.websocket_connect(f"/v1/ws?token={_token()}") as ws:
        ws.send_json({"id": "1", "method": method, "data": {}})
        assert ws.receive_json() == {"method": "error", "id": "1", "error": "missing_topic"}


def test_reauth_with_a_valid_token_acks(client):
    with client.websocket_connect(f"/v1/ws?token={_token()}") as ws:
        ws.send_json({"id": "1", "method": "reauth", "data": {"token": _token()}})
        assert ws.receive_json() == {"method": "ack", "id": "1", "reauth": True}


def test_reauth_with_an_invalid_token_errors_then_closes_4403(client):
    with client.websocket_connect(f"/v1/ws?token={_token()}") as ws:
        ws.send_json({"id": "1", "method": "reauth", "data": {"token": "garbage"}})
        assert ws.receive_json() == {"method": "error", "id": "1", "error": "invalid_token"}
        with pytest.raises(Exception) as exc_info:
            ws.receive_text()
    assert "4403" in str(exc_info.value) or getattr(exc_info.value, "code", None) == 4403
