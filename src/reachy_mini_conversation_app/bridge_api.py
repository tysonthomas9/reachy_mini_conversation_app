"""Bridge API for external messaging integration (OpenClaw, etc.).

Exposes HTTP and WebSocket endpoints on the Reachy Mini settings app so
external services can inject text/images into the live OpenAI Realtime
session and receive transcript streams.

Follows the same ``mount_*_routes()`` pattern as ``headless_personality_ui.py``.
"""

import hmac
import json
import asyncio
import logging
from typing import Any, Callable, Optional

from fastapi import FastAPI

from .openai_realtime import OpenaiRealtimeHandler


logger = logging.getLogger(__name__)


class BridgeState:
    """Shared state for the bridge API."""

    def __init__(self, secret: Optional[str] = None) -> None:
        """Initialize bridge state with an optional shared secret."""
        self._secret = secret
        self._ws_clients: set[Any] = set()

    def verify_auth(self, provided: Optional[str]) -> bool:
        """Return True if auth passes.

        When no secret is configured, all requests are allowed.
        """
        if self._secret is None:
            return True
        if provided is None:
            return False
        return hmac.compare_digest(self._secret, provided)

    @property
    def ws_client_count(self) -> int:
        """Return number of connected WebSocket clients."""
        return len(self._ws_clients)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a JSON message to all connected WebSocket clients."""
        if not self._ws_clients:
            return
        data = json.dumps(message)
        closed: set[Any] = set()
        for ws in self._ws_clients:
            try:
                await ws.send_text(data)
            except Exception:
                closed.add(ws)
        self._ws_clients -= closed


def mount_bridge_routes(
    app: FastAPI,
    handler: OpenaiRealtimeHandler,
    get_loop: Callable[[], Optional[asyncio.AbstractEventLoop]],
    bridge_state: BridgeState,
) -> None:
    """Register bridge endpoints on a FastAPI app."""
    try:
        from fastapi import Header
        from pydantic import BaseModel
        from fastapi.responses import JSONResponse
        from starlette.websockets import WebSocket, WebSocketDisconnect
    except Exception:  # pragma: no cover
        return

    MAX_IMAGE_B64_LEN = 5 * 1024 * 1024  # ~3.75 MB decoded

    class InjectPayload(BaseModel):
        text: Optional[str] = None
        image_b64: Optional[str] = None
        response_instructions: Optional[str] = None

    @app.post("/bridge/inject")
    async def _inject(
        payload: InjectPayload,
        x_bridge_secret: Optional[str] = Header(None),
    ) -> JSONResponse:
        if not bridge_state.verify_auth(x_bridge_secret):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        loop = get_loop()
        if loop is None:
            return JSONResponse({"ok": False, "error": "loop_unavailable"}, status_code=503)

        if not payload.text and not payload.image_b64:
            return JSONResponse({"ok": False, "error": "provide text or image_b64"}, status_code=400)

        if payload.image_b64 and len(payload.image_b64) > MAX_IMAGE_B64_LEN:
            return JSONResponse({"ok": False, "error": "image_b64 exceeds 5 MB limit"}, status_code=413)

        async def _do_inject() -> str:
            if not handler.connection:
                return "no_connection"

            # Inject text message
            if payload.text:
                await handler.connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": payload.text}],
                    },
                )

            # Inject image
            if payload.image_b64:
                await handler.connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": f"data:image/jpeg;base64,{payload.image_b64}",
                            },
                        ],
                    },
                )

            # Trigger a response
            response_kwargs: dict[str, Any] = {}
            if payload.response_instructions:
                response_kwargs["instructions"] = payload.response_instructions
            await handler.connection.response.create(
                response=response_kwargs if response_kwargs else None,
            )
            return "ok"

        try:
            fut = asyncio.run_coroutine_threadsafe(_do_inject(), loop)
            status = fut.result(timeout=10)
            return JSONResponse({"ok": True, "status": status})
        except Exception as e:
            logger.error("Bridge inject failed: %s", e)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.get("/bridge/status")
    def _status() -> JSONResponse:
        connected = handler.connection is not None
        return JSONResponse(
            {
                "connected": connected,
                "ws_clients": bridge_state.ws_client_count,
            }
        )

    @app.websocket("/bridge/ws")
    async def _ws(websocket: WebSocket) -> None:
        secret = websocket.query_params.get("secret")
        await websocket.accept()
        if not bridge_state.verify_auth(secret):
            await websocket.close(code=4001, reason="unauthorized")
            return
        bridge_state._ws_clients.add(websocket)
        logger.info("Bridge WS client connected (%d total)", bridge_state.ws_client_count)
        try:
            # Keep connection alive — client only receives, never sends
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            bridge_state._ws_clients.discard(websocket)
            logger.info("Bridge WS client disconnected (%d remaining)", bridge_state.ws_client_count)
