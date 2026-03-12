from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class DeribitWSClient:
    """
    Async WebSocket client for Deribit.

    Usage:
        client = DeribitWSClient(url, on_message=handler)
        await client.connect()
        await client.subscribe(["deribit_price_index.eth_usd"])
        await client.run_forever()
    """

    def __init__(
        self,
        url: str,
        client_id: str = "",
        client_secret: str = "",
        on_message: Optional[Callable[[dict], None]] = None,
        heartbeat_interval: int = 30,
    ) -> None:
        self.url = url
        self.client_id = client_id
        self.client_secret = client_secret
        self.on_message = on_message or (lambda msg: None)
        self.heartbeat_interval = heartbeat_interval
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._subscriptions: list[str] = []
        self._access_token: Optional[str] = None
        self._running = False

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.url, ping_interval=None)
        self._running = True
        logger.info("WebSocket connected to %s", self.url)
        if self.client_id:
            await self._authenticate()
        asyncio.create_task(self._reader())
        asyncio.create_task(self._heartbeat())

    async def _send(self, method: str, params: dict) -> dict:
        req_id = self._next_id()
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps(msg))
        return await asyncio.wait_for(fut, timeout=15)

    async def _authenticate(self) -> None:
        result = await self._send(
            "public/auth",
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        self._access_token = result["access_token"]
        logger.info("WebSocket authenticated")

    async def subscribe(self, channels: list[str]) -> None:
        method = "private/subscribe" if self._access_token else "public/subscribe"
        params: dict[str, Any] = {"channels": channels}
        if self._access_token:
            params["access_token"] = self._access_token
        await self._send(method, params)
        self._subscriptions.extend(channels)
        logger.info("Subscribed to %s", channels)

    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                    if "id" in msg and msg["id"] in self._pending:
                        fut = self._pending.pop(msg["id"])
                        if "error" in msg:
                            fut.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            fut.set_result(msg.get("result", {}))
                    elif "method" in msg and msg["method"] == "subscription":
                        self.on_message(msg.get("params", {}))
                    elif msg.get("method") == "heartbeat":
                        await self._ws.send(
                            json.dumps({"jsonrpc": "2.0", "method": "public/test", "params": {}})
                        )
                except Exception as exc:
                    logger.error("Error processing WS message: %s", exc)
        except ConnectionClosed:
            logger.warning("WebSocket connection closed")
            self._running = False

    async def _heartbeat(self) -> None:
        while self._running:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                if self._ws and not self._ws.closed:
                    await self._send(
                        "public/set_heartbeat", {"interval": self.heartbeat_interval}
                    )
            except Exception as exc:
                logger.error("Heartbeat failed: %s", exc)

    async def run_forever(self) -> None:
        while self._running:
            await asyncio.sleep(1)

    async def close(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()


class OptionChainStreamer:
    """
    Subscribes to all ETH option tickers via WebSocket and
    calls on_quote(channel, data) for each update.
    """

    TICKER_CHANNEL = "ticker.{instrument}.100ms"

    def __init__(
        self,
        ws_client: DeribitWSClient,
        instruments: list[str],
        on_quote: Callable[[str, dict], None],
    ) -> None:
        self.ws = ws_client
        self.instruments = instruments
        self.on_quote = on_quote

    async def start(self) -> None:
        channels = [self.TICKER_CHANNEL.format(instrument=i) for i in self.instruments]
        self.ws.on_message = self._dispatch
        # subscribe in batches to avoid overwhelming the connection
        batch_size = 50
        for i in range(0, len(channels), batch_size):
            await self.ws.subscribe(channels[i : i + batch_size])
            await asyncio.sleep(0.1)

    def _dispatch(self, params: dict) -> None:
        channel = params.get("channel", "")
        data = params.get("data", {})
        self.on_quote(channel, data)
