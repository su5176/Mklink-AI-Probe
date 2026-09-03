"""Authenticated binary WebSocket data plane for live MKLink streams."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from dataclasses import asdict
from typing import Dict, Mapping, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mklink.remote.stream_hub import StreamHub
from mklink.remote.stream_protocol import Frame, StreamType, encode_frame

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 1.0
AUTH_TIMEOUT_SECONDS = 5.0
MAX_BATCHES_PER_CLIENT = 64

STREAM_TYPES: Mapping[str, StreamType] = {
    "systemview": StreamType.SYSTEMVIEW,
    "vofa": StreamType.WAVEFORM,
    "rtt": StreamType.RTT_RAW,
    "rtt-terminal": StreamType.RTT_RAW,
    "serial": StreamType.SERIAL,
    "superwatch": StreamType.SUPERWATCH,
}


def create_stream_registry() -> Dict[str, StreamHub]:
    """Create the per-application stream hubs."""
    return {
        name: StreamHub(max_batches_per_client=MAX_BATCHES_PER_CLIENT)
        for name in STREAM_TYPES
    }


async def _authenticate(websocket: WebSocket, auth_token: Optional[str]) -> bool:
    if not auth_token:
        return True
    authorization = websocket.headers.get("authorization")
    if authorization is not None:
        return hmac.compare_digest(authorization, f"Bearer {auth_token}")
    try:
        message = await asyncio.wait_for(
            websocket.receive(), timeout=AUTH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return False
    if message.get("type") != "websocket.receive":
        return False
    text = message.get("text")
    if not isinstance(text, str):
        return False
    try:
        auth = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(auth, dict):
        return False
    params = auth.get("params")
    if not isinstance(params, dict):
        params = {}
    token = auth.get("token") or params.get("token")
    return token == auth_token


def _encoded_data_frame(
    stream_type: StreamType, batch,
) -> bytes:
    batch_stream_type = batch.stream_type or stream_type
    return encode_frame(Frame(
        stream_type=batch_stream_type,
        flags=batch.flags,
        stream_id=int(stream_type),
        sequence=batch.sequence,
        timestamp_ns=batch.timestamp_ns,
        item_count=batch.item_count,
        payload=batch.payload,
    ))


def _encoded_status_frame(stream_type: StreamType, hub: StreamHub) -> bytes:
    stats = hub.status_frame()
    payload = json.dumps(
        asdict(stats), separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return encode_frame(Frame(
        stream_type=StreamType.CONTROL,
        flags=0,
        stream_id=int(stream_type),
        sequence=stats.last_sequence,
        timestamp_ns=time.time_ns(),
        item_count=0,
        payload=payload,
    ))


async def stream_websocket(
    websocket: WebSocket,
    stream_name: str,
    registry: Mapping[str, StreamHub],
    stream_types: Mapping[str, StreamType],
    auth_token: Optional[str],
) -> None:
    """Serve one stream subscriber until its WebSocket disconnects."""
    await websocket.accept()
    hub = registry.get(stream_name)
    stream_type = stream_types.get(stream_name)
    if hub is None or stream_type is None:
        await websocket.close(code=1008, reason="Unknown stream")
        return
    if not await _authenticate(websocket, auth_token):
        await websocket.close(code=1008, reason="Unauthorized")
        return

    # Keep the CONTROL frame as the subscription-ready barrier before a
    # producer's subscribe callback can enqueue retained metadata.
    try:
        await websocket.send_bytes(_encoded_status_frame(stream_type, hub))
    except Exception as exc:
        logger.debug("Binary stream WebSocket closed before subscribe: %s", exc)
        return
    queue = hub.subscribe()
    try:
        while True:
            try:
                batch = await asyncio.wait_for(
                    queue.get(), timeout=HEARTBEAT_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                await websocket.send_bytes(
                    _encoded_status_frame(stream_type, hub)
                )
                continue
            try:
                await websocket.send_bytes(
                    _encoded_data_frame(stream_type, batch)
                )
            finally:
                queue.task_done()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("Binary stream WebSocket closed: %s", exc)
    finally:
        hub.unsubscribe(queue)


def create_stream_router(
    registry: Mapping[str, StreamHub],
    stream_types: Mapping[str, StreamType],
    auth_token: Optional[str],
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/streams/{stream_name}")
    async def stream_socket(websocket: WebSocket, stream_name: str):
        await stream_websocket(
            websocket, stream_name, registry, stream_types, auth_token,
        )

    return router
