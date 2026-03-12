from __future__ import annotations

import time
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class DeribitRESTClient:
    """
    Thin wrapper around the Deribit REST API v2.
    Handles authentication (client_credentials flow) and token refresh.
    """

    def __init__(self, base_url: str, client_id: str = "", client_secret: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self) -> None:
        params = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        data = self._get("/api/v2/public/auth", params)
        result = data["result"]
        self._access_token = result["access_token"]
        self._token_expires_at = time.time() + result["expires_in"] - 60

    def _ensure_auth(self) -> None:
        if not self.client_id:
            return
        if self._access_token is None or time.time() >= self._token_expires_at:
            self._authenticate()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        resp = self._session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _private_get(self, path: str, params: Optional[dict] = None) -> dict:
        self._ensure_auth()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        url = f"{self.base_url}{path}"
        resp = self._session.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Deribit API error: {data['error']}")
        return data

    def _private_post(self, path: str, payload: dict) -> dict:
        self._ensure_auth()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        url = f"{self.base_url}{path}"
        resp = self._session.post(url, json=payload, headers=headers, timeout=15)
        if not resp.ok:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise RuntimeError(f"Deribit API {resp.status_code} error: {body}")
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Deribit API error: {data['error']}")
        return data

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    def get_instruments(self, currency: str = "ETH", kind: str = "option", expired: bool = False) -> list[dict]:
        data = self._get(
            "/api/v2/public/get_instruments",
            {"currency": currency, "kind": kind, "expired": str(expired).lower()},
        )
        return data.get("result", [])

    def get_order_book(self, instrument_name: str, depth: int = 5) -> dict:
        data = self._get(
            "/api/v2/public/get_order_book",
            {"instrument_name": instrument_name, "depth": depth},
        )
        return data.get("result", {})

    def get_ticker(self, instrument_name: str) -> dict:
        data = self._get(
            "/api/v2/public/ticker",
            {"instrument_name": instrument_name},
        )
        return data.get("result", {})

    def get_index_price(self, index_name: str = "eth_usd") -> float:
        data = self._get(
            "/api/v2/public/get_index_price",
            {"index_name": index_name},
        )
        return data["result"]["index_price"]

    def get_historical_volatility(self, currency: str = "ETH") -> list[dict]:
        data = self._get(
            "/api/v2/public/get_historical_volatility",
            {"currency": currency},
        )
        return data.get("result", [])

    def get_option_chain(self, currency: str = "ETH",
                         dte_max: int = 30,
                         spot_price: float = 0.0,
                         strike_range_pct: float = 0.0) -> list[dict]:
        """
        Fetch option chain tickers, pre-filtered by expiry and optionally strike range.

        dte_max          — skip instruments expiring more than this many days out (default 30)
        spot_price       — if >0, also filter strikes within ±strike_range_pct of spot
        strike_range_pct — e.g. 0.30 keeps strikes within ±30% of spot
        """
        instruments = self.get_instruments(currency=currency, kind="option")
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        cutoff_ms = now_ms + dte_max * 86_400_000

        filtered = []
        for inst in instruments:
            if inst["expiration_timestamp"] > cutoff_ms:
                continue
            if spot_price > 0 and strike_range_pct > 0:
                lo = spot_price * (1 - strike_range_pct)
                hi = spot_price * (1 + strike_range_pct)
                if not (lo <= inst["strike"] <= hi):
                    continue
            filtered.append(inst)

        logger.info("get_option_chain: %d/%d instruments after filtering (dte_max=%d)",
                    len(filtered), len(instruments), dte_max)

        results = []
        for inst in filtered:
            try:
                ticker = self.get_ticker(inst["instrument_name"])
                ticker["instrument_name"] = inst["instrument_name"]
                ticker["strike"] = inst["strike"]
                ticker["expiration_timestamp"] = inst["expiration_timestamp"]
                ticker["option_type"] = inst["option_type"]
                results.append(ticker)
            except Exception as exc:
                logger.warning("Failed to fetch ticker %s: %s", inst["instrument_name"], exc)
            time.sleep(0.05)   # 50ms between calls = max 20 req/s, well within limits
        return results

    # ------------------------------------------------------------------
    # Private endpoints
    # ------------------------------------------------------------------

    def get_account_summary(self, currency: str = "ETH") -> dict:
        data = self._private_get(
            "/api/v2/private/get_account_summary",
            {"currency": currency, "extended": "true"},
        )
        return data.get("result", {})

    def get_positions(self, currency: str = "ETH") -> list[dict]:
        data = self._private_get(
            "/api/v2/private/get_positions",
            {"currency": currency, "kind": "option"},
        )
        return data.get("result", [])

    def get_open_orders(self, currency: str = "ETH") -> list[dict]:
        data = self._private_get(
            "/api/v2/private/get_open_orders_by_currency",
            {"currency": currency, "kind": "option"},
        )
        return data.get("result", [])

    def place_order(
        self,
        instrument_name: str,
        side: str,
        amount: float,
        price: float,
        order_type: str = "limit",
        label: str = "",
    ) -> dict:
        path = f"/api/v2/private/{'buy' if side == 'buy' else 'sell'}"
        # Round price to Deribit ETH option tick size (0.0001 ETH)
        price = round(price, 4)
        # Deribit requires integer contract amounts for ETH options (min 1)
        amount = max(1, int(round(amount)))
        params = {
            "instrument_name": instrument_name,
            "amount": amount,
            "type": order_type,
            "price": price,
        }
        if label:
            params["label"] = label
        # Deribit REST API uses GET with query params for buy/sell
        data = self._private_get(path, params)
        return data.get("result", {})

    def cancel_order(self, order_id: str) -> dict:
        data = self._private_get("/api/v2/private/cancel", {"order_id": order_id})
        return data.get("result", {})

    def cancel_all_by_instrument(self, instrument_name: str) -> dict:
        data = self._private_post(
            "/api/v2/private/cancel_all_by_instrument",
            {"instrument_name": instrument_name, "type": "all"},
        )
        return data.get("result", {})

    def get_order_state(self, order_id: str) -> dict:
        data = self._private_get(
            "/api/v2/private/get_order_state",
            {"order_id": order_id},
        )
        return data.get("result", {})
