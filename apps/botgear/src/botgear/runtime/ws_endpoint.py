# unmanaged
"""WebSocket endpoint for the runtime bus.

Mounts ``GET /v1/ws`` on the FastAPI app via ``register_ws_routes(app)``,
which is called from ``botgear.yml`` ``api.extend``. One connection
per user. Frames are JSON, matching the shape of the ``message`` model
in the yml:

    { "id": "<uuid>", "method": "<verb>", "data": { ... } }

Methods (client → server):
    subscribe   { topic }
    unsubscribe { topic }
    publish     { topic, payload, retained?: bool }
    request     { topic, payload, reply_to } — same as publish; sender
                                               supplies reply_to so a
                                               responder can address them.
    reauth      { token } — replace the connection's token with a freshly
                            refreshed one, extending it past the original's
                            exp without dropping the socket. Acks with
                            {method: "ack", reauth: true}; an invalid token
                            closes the connection (_CLOSE_BAD_TOKEN).

Server → client frames use ``method: "message"`` for bus deliveries,
``method: "ack"`` for command receipts, and ``method: "error"`` for
malformed frames.

Auth is JWT in the ``?token=`` query parameter. Browsers cannot set
headers on ``new WebSocket(url)``, so the query carries the token. Same
shared secret + algorithm (HS256) as ``packages/auth/local_auth.py``.

The token is only verified at handshake — after ``accept()`` the
connection stays open on its own terms, INDEPENDENT of that token's
``exp``. That's deliberate: a persistent control channel (this is a
robotics app; think an open teleop session) shouldn't get yanked out from
under an operator just because a clock ticked past an access token's
1-hour lifetime mid-session. What keeps this from being an open-ended
security hole is the ``reauth`` method + the expiry watchdog below: the
client is expected to push a freshly-refreshed token over the SAME socket
before the current one lapses (see botgear_ui's wsClient.reauth(), driven
by AuthContext's proactive-refresh timer); if nothing fresher arrives
within ``_REAUTH_GRACE_SECONDS`` of the current token's ``exp``, the
watchdog force-closes the connection with ``_CLOSE_REAUTH_REQUIRED``. So
the connection survives ROUTINE token rotation transparently, but still
gets cut off — same as a fresh connect would be — if the client stops
proving it holds a valid credential (revoked account, crashed tab that
can no longer refresh, refresh token itself expired).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

import jwt

from botgear.runtime.bus import Bus, get_bus


logger = logging.getLogger(__name__)


# WebSocket close codes (RFC 6455 — application range starts at 4000).
_CLOSE_NO_TOKEN = 4401           # like HTTP 401 for the WS world.
_CLOSE_BAD_TOKEN = 4403          # like HTTP 403 — also used for a bad `reauth`.
_CLOSE_REAUTH_REQUIRED = 4408    # an open connection's token lapsed with no reauth in time.

# Grace window after the current token's `exp` before the watchdog force-
# closes a connection that never sent a fresh `reauth`. Generous on
# purpose: the client's own proactive refresh fires at 80% of the token's
# lifetime (see botgear_ui's scheduleProactiveRefresh), so under normal
# operation a `reauth` lands with most of this window to spare. This is a
# backstop against a client that's stopped refreshing entirely, not the
# routine path.
_REAUTH_GRACE_SECONDS = 5 * 60


def _jwt_secret() -> str:
    # Delegate to the single source (auth.jwt_util) so the env var + dev fallback
    # stay normalized instead of re-typed here.
    from auth import get_jwt_secret
    return get_jwt_secret()


def _decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        logger.info("ws.auth_rejected reason=%s", exc.__class__.__name__)
        return None


def _payload_from_api_key(api_key: str) -> Optional[dict]:
    """Adapt an ``api_client`` X-API-Key secret into the same payload
    shape ``_decode_token`` produces, so every downstream read of
    ``payload`` (user id, roles, exp) works unchanged regardless of
    which auth path was used. Used for revocable, non-expiring
    peer-to-peer federation credentials (see peer_manager.py) as an
    alternative to the shared JWT_SECRET_KEY subprocess token — each
    peer can carry its own api_client row instead of every runtime in
    the fleet trusting one global secret.

    No ``exp`` key, same as subprocess tokens — api_client rows don't
    expire; revocation is deleting the row, checked fresh on every
    connect/reconnect (no cache on this path, see
    validate_api_client_secret's docstring)."""
    from botgear.services.api_client_service import validate_api_client_secret
    try:
        principal = validate_api_client_secret(api_key)
    except Exception:  # noqa: BLE001
        logger.exception("ws: api_key validation raised")
        return None
    if not principal:
        logger.info("ws.auth_rejected reason=invalid_api_key")
        return None
    client_id = principal.get("id") or "api-client"
    return {
        "sub": client_id,
        "user": {
            "id": client_id,
            "email": f"{principal.get('name') or client_id}@api-client",
            "roles": principal.get("roles") or [],
            "status": "active",
        },
    }


async def _ws_send_json(ws: WebSocket, frame: dict) -> None:
    """Send a frame but tolerate a half-closed socket."""
    if ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.send_text(json.dumps(frame))
    except (WebSocketDisconnect, RuntimeError):
        # Client disconnected mid-write or socket is closing — give up
        # silently; the main read-loop will catch the disconnect and
        # tear the connection down.
        pass


async def _pump_bus_to_ws(
    bus: Bus,
    topic: str,
    subscriber_id: str,
    ws: WebSocket,
) -> None:
    """Forward every message on ``topic`` to ``ws`` as a frame.

    Owned by the WS handler — cancelled on unsubscribe or disconnect.
    """
    try:
        async for msg in bus.subscribe(topic, subscriber_id=subscriber_id):
            if msg.topic == "__terminate__":
                return
            await _ws_send_json(
                ws,
                {
                    "method": "message",
                    "topic": msg.topic,
                    "payload": msg.payload,
                    "sender_id": msg.sender_id,
                    "reply_to": msg.reply_to,
                    "timestamp": msg.timestamp,
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ws.pump_error topic=%s subscriber=%s", topic, subscriber_id)


async def _watch_token_expiry(
    websocket: WebSocket,
    conn_state: Dict[str, Optional[float]],
    connection_id: str,
) -> None:
    """Force-close the connection if its current token's ``exp`` (extended
    by each successful `reauth` — see the endpoint's message loop) passes
    ``_REAUTH_GRACE_SECONDS`` with nothing fresher presented.

    Re-reads ``conn_state["token_exp"]`` on every wake so a `reauth` that
    lands mid-sleep reschedules the deadline with no separate signal
    mechanism needed — a short poll loop is simpler than an Event/Condition
    here and plenty timely at this grain (sleeps in <=60s increments).
    """
    try:
        while True:
            exp = conn_state.get("token_exp")
            if exp is None:
                return  # token had no exp claim — nothing to enforce.
            remaining = (exp + _REAUTH_GRACE_SECONDS) - time.time()
            if remaining <= 0:
                logger.info("ws.reauth_timeout conn=%s", connection_id)
                await websocket.close(code=_CLOSE_REAUTH_REQUIRED, reason="reauth required")
                return
            await asyncio.sleep(min(remaining, 60))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ws.watch_expiry_error conn=%s", connection_id)


def register_ws_routes(app: FastAPI) -> None:
    """Attach ``GET /v1/ws`` to the FastAPI app.

    Called from botgear.yml ``api.extend`` so route registration stays
    with the rest of the app's generated routes.
    """
    bus = get_bus()

    @app.websocket("/v1/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        token = websocket.query_params.get("token")
        api_key = websocket.query_params.get("api_key")
        if not token and not api_key:
            # accept() BEFORE close() is deliberate: a WS close before the
            # handshake completes surfaces to the browser as a raw HTTP
            # 403 at the transport level, not a WS close FRAME — so
            # WebSocket.onclose never sees our code (4401/4403), just a
            # generic abnormal-closure 1006. That's indistinguishable from
            # a network blip client-side, which defeats the whole point of
            # having a distinct auth-rejection code: wsClient.ts can't tell
            # "your token is dead, don't just retry" from "server bounced,
            # go ahead and retry" without it (see wsClient.ts's
            # AUTH_CLOSE_CODES). Accepting first makes the rejection a
            # real, inspectable close event either way.
            await websocket.accept()
            await websocket.close(code=_CLOSE_NO_TOKEN, reason="missing token")
            return
        # ``api_key`` is a second, independent credential type (an
        # api_client row's secret) — see _payload_from_api_key. Tried
        # when ``token`` wasn't supplied, not as a fallback after a bad
        # JWT: a caller picks one scheme per connection, same as the
        # HTTP X-API-Key path never also accepts a bearer token.
        payload = _decode_token(token) if token else _payload_from_api_key(api_key)
        if not payload:
            await websocket.accept()
            await websocket.close(code=_CLOSE_BAD_TOKEN, reason="bad token")
            return

        await websocket.accept()
        user = payload.get("user") or {}
        user_id = user.get("id") or payload.get("sub") or "anonymous"
        # Per-connection subscriber id keeps two tabs from sharing queues.
        connection_id = f"{user_id}#{uuid.uuid4().hex[:8]}"
        pumps: Dict[str, asyncio.Task] = {}
        # Mutable box (not a bare variable) so _watch_token_expiry — running
        # in its own task — sees each `reauth`'s update without a separate
        # signal mechanism (see that function's docstring).
        conn_state: Dict[str, Optional[float]] = {"token_exp": payload.get("exp")}
        watchdog_task = asyncio.create_task(
            _watch_token_expiry(websocket, conn_state, connection_id),
            name=f"ws_reauth_watchdog:{connection_id}",
        )

        logger.info("ws.connect user=%s conn=%s", user_id, connection_id)

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    await _ws_send_json(
                        websocket,
                        {"method": "error", "error": "invalid_json"},
                    )
                    continue

                method = frame.get("method")
                data = frame.get("data") or {}
                topic = data.get("topic") if isinstance(data, dict) else None
                frame_id = frame.get("id")

                if method == "subscribe":
                    if not topic:
                        await _ws_send_json(
                            websocket,
                            {"method": "error", "id": frame_id, "error": "missing_topic"},
                        )
                        continue
                    if topic in pumps:
                        # idempotent: ack but don't double-subscribe.
                        await _ws_send_json(
                            websocket,
                            {"method": "ack", "id": frame_id, "topic": topic, "subscribed": True},
                        )
                        continue
                    task = asyncio.create_task(
                        _pump_bus_to_ws(bus, topic, connection_id, websocket),
                        name=f"ws_pump:{connection_id}:{topic}",
                    )
                    pumps[topic] = task
                    await _ws_send_json(
                        websocket,
                        {"method": "ack", "id": frame_id, "topic": topic, "subscribed": True},
                    )

                elif method == "unsubscribe":
                    task = pumps.pop(topic, None) if topic else None
                    if task is not None:
                        task.cancel()
                    await _ws_send_json(
                        websocket,
                        {"method": "ack", "id": frame_id, "topic": topic, "subscribed": False},
                    )

                elif method == "publish":
                    if not topic:
                        await _ws_send_json(
                            websocket,
                            {"method": "error", "id": frame_id, "error": "missing_topic"},
                        )
                        continue
                    payload_value = data.get("payload")
                    retained = bool(data.get("retained", False))
                    count = await bus.publish(
                        topic,
                        payload_value,
                        sender_id=connection_id,
                        retained=retained,
                    )
                    await _ws_send_json(
                        websocket,
                        {
                            "method": "ack",
                            "id": frame_id,
                            "topic": topic,
                            "delivered": count,
                        },
                    )

                elif method == "list_topics":
                    # Inspector polls this to discover what's flowing on the
                    # bus. Returns active topic names with their current
                    # subscriber count, retained-message flag, dropped
                    # count, AND the parsed identity of each subscriber
                    # (Layer 3 — so the UI can say *who* is listening,
                    # not just how many). ``patterns`` carries wildcard
                    # subscriptions separately so the UI can show them
                    # distinct from concrete topics.
                    topics_out = bus.list_topics_detail()
                    patterns_out = sorted(bus.patterns())
                    await _ws_send_json(
                        websocket,
                        {
                            "method": "topics",
                            "id": frame_id,
                            "topics": topics_out,
                            "patterns": patterns_out,
                        },
                    )

                elif method == "request":
                    # Same write path as publish; sender supplies reply_to
                    # so the responder can route a reply via publish.
                    if not topic:
                        await _ws_send_json(
                            websocket,
                            {"method": "error", "id": frame_id, "error": "missing_topic"},
                        )
                        continue
                    reply_to = frame.get("reply_to") or data.get("reply_to")
                    payload_value = data.get("payload")
                    count = await bus.publish(
                        topic,
                        payload_value,
                        sender_id=connection_id,
                        reply_to=reply_to,
                    )
                    await _ws_send_json(
                        websocket,
                        {
                            "method": "ack",
                            "id": frame_id,
                            "topic": topic,
                            "delivered": count,
                        },
                    )

                elif method == "reauth":
                    # See the module docstring + _watch_token_expiry: this
                    # is what lets a persistent connection outlive its
                    # ORIGINAL token without becoming a standing security
                    # gap — the client pushes a freshly-refreshed token
                    # before the current one lapses, we re-verify it and
                    # extend the deadline the watchdog is enforcing.
                    new_token = data.get("token") if isinstance(data, dict) else None
                    new_payload = _decode_token(new_token) if new_token else None
                    if not new_payload:
                        await _ws_send_json(
                            websocket,
                            {"method": "error", "id": frame_id, "error": "invalid_token"},
                        )
                        await websocket.close(code=_CLOSE_BAD_TOKEN, reason="reauth rejected")
                        return
                    conn_state["token_exp"] = new_payload.get("exp")
                    logger.debug("ws.reauth conn=%s", connection_id)
                    await _ws_send_json(
                        websocket,
                        {"method": "ack", "id": frame_id, "reauth": True},
                    )

                else:
                    await _ws_send_json(
                        websocket,
                        {"method": "error", "id": frame_id, "error": "unknown_method", "method_received": method},
                    )

        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("ws.handler_error conn=%s", connection_id)
        finally:
            watchdog_task.cancel()
            for task in pumps.values():
                task.cancel()
            # Wait briefly so the iterators' `finally` cleanup runs.
            await asyncio.gather(watchdog_task, *pumps.values(), return_exceptions=True)
            await bus.unsubscribe_all(connection_id)
            logger.info("ws.disconnect conn=%s", connection_id)
