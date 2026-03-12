"""
WSPositionMonitor
=================
Monitors an open spread position via Deribit WebSocket.
Subscribes to ticker.{instrument}.100ms for each active leg.
Sets exit_reason the moment TP or SL is crossed — sub-second reaction time.

The main bot loop checks monitor.exit_reason every WS_CHECK_SECONDS (5s).
If no WS update arrives in FALLBACK_SECS (90s), the REST polling loop takes over.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from typing import Optional

from src.deribit.ws_client import DeribitWSClient
from src.data.models import IronCondor

logger = logging.getLogger(__name__)

FALLBACK_SECS = 90  # treat WS as dead if no update in this many seconds


class WSPositionMonitor:
    """
    Runs in a background daemon thread.
    Thread-safe: exit_reason and ws_alive can be read from any thread.
    """

    def __init__(
        self,
        spread: IronCondor,
        take_profit_pct: float,
        stop_loss_multiplier: float,
        ws_url: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self.spread               = spread
        self.take_profit_pct      = take_profit_pct
        self.stop_loss_multiplier = stop_loss_multiplier
        self.ws_url               = ws_url
        self.client_id            = client_id
        self.client_secret        = client_secret

        self._mark_prices: dict[str, float] = {}
        self._last_update: float            = 0.0
        self._lock                          = threading.Lock()
        self._exit_reason: Optional[str]    = None
        self._stop_flag                     = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public interface (main thread)
    # ------------------------------------------------------------------

    @property
    def exit_reason(self) -> Optional[str]:
        with self._lock:
            return self._exit_reason

    @property
    def ws_alive(self) -> bool:
        """True if a WS update was received within FALLBACK_SECS."""
        return self._last_update > 0 and (time.time() - self._last_update) < FALLBACK_SECS

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"ws-monitor-{self.spread.id[:8]}"
        )
        self._thread.start()
        logger.info("WSPositionMonitor started | spread=%s", self.spread.id)

    def stop(self) -> None:
        self._stop_flag.set()
        logger.info("WSPositionMonitor stopped | spread=%s", self.spread.id)

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._monitor())
        except Exception as exc:
            logger.error("WSPositionMonitor error: %s", exc)
        finally:
            loop.close()

    async def _monitor(self) -> None:
        ws = DeribitWSClient(
            url=self.ws_url,
            client_id=self.client_id,
            client_secret=self.client_secret,
        )
        try:
            await ws.connect()
        except Exception as exc:
            logger.error("WSPositionMonitor: connect failed: %s", exc)
            return

        instruments = [
            leg.instrument_name
            for leg in self.spread.legs
            if leg.quantity > 0 and leg.instrument_name != "STUB"
        ]
        channels = [f"ticker.{inst}.100ms" for inst in instruments]
        ws.on_message = self._on_message

        try:
            await ws.subscribe(channels)
            logger.info("WSPositionMonitor: subscribed %s", channels)
            while not self._stop_flag.is_set():
                await asyncio.sleep(0.5)
        except Exception as exc:
            logger.error("WSPositionMonitor subscription error: %s", exc)
        finally:
            await ws.close()

    # ------------------------------------------------------------------
    # WS callback (background thread)
    # ------------------------------------------------------------------

    def _on_message(self, params: dict) -> None:
        channel = params.get("channel", "")
        data    = params.get("data", {})
        if not channel.startswith("ticker."):
            return
        inst = channel.split(".")[1]
        mark = data.get("mark_price")
        if mark is None:
            return
        with self._lock:
            self._mark_prices[inst] = float(mark)
            self._last_update = time.time()
            self._evaluate()

    def _evaluate(self) -> None:
        """Called under lock. Computes unrealized P&L and sets exit_reason if triggered."""
        if self._exit_reason:
            return  # already triggered

        instruments = [
            leg.instrument_name
            for leg in self.spread.legs
            if leg.quantity > 0 and leg.instrument_name != "STUB"
        ]
        if not all(inst in self._mark_prices for inst in instruments):
            return  # wait until all legs have a price

        quote_map = {
            inst: SimpleNamespace(mid=self._mark_prices[inst])
            for inst in instruments
        }
        upnl = self.spread.unrealized_pnl(quote_map)

        if upnl >= self.spread.credit_received * self.take_profit_pct:
            self._exit_reason = f"take_profit [ws] pnl={upnl:+.5f}"
        elif upnl <= -(self.spread.credit_received * self.stop_loss_multiplier):
            self._exit_reason = f"stop_loss [ws] pnl={upnl:+.5f}"
