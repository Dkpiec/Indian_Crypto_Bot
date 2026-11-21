"""
Native Binance HTTP client with dual-endpoint routing.
- Authenticated trading/account endpoints → https://papi.binance.com/papi/v1/*
- Public market data (klines, ticker) → https://fapi.binance.com/fapi/v1/*

The papi gateway only serves portfolio-margin account/order endpoints.
Public market data does NOT exist on papi and must stay on fapi.
"""

import hashlib
import hmac
import logging
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)


class PapiClient:
    """
    Dual-endpoint HTTP client for Binance.
    
    - Authenticated calls (account, positionRisk, order, openOrders) → papi
    - Public market data (klines, ticker/price) → fapi (no auth required)
    """

    PAPI_BASE = "https://papi.binance.com/papi/v1"
    FAPI_BASE = "https://fapi.binance.com/fapi/v1"

    # Endpoints that exist ONLY on papi (authenticated trading/account)
    PAPI_ONLY_PATHS = {
        "/account",
        "/positionRisk",
        "/order",
        "/openOrders",
    }

    # Endpoints that exist ONLY on fapi (public market data)
    FAPI_ONLY_PATHS = {
        "/klines",
        "/ticker/price",
        "/ticker/24hr",
    }

    # Endpoints that don't exist on papi for portfolio margin accounts — mock them
    MOCK_ENDPOINTS = {
        "/positionSide/dual": {"dualSidePosition": False},
        "/multiAssetsMargin": {"multiAssetsMargin": False},
        "/leverageBracket": [],
        "/positionRisk": [],
        "/openOrders": [],
    }

    def __init__(self, api_key: str, secret_key: str, recv_window: int = 30000):
        self.api_key = api_key
        self.secret_key = secret_key.encode("utf-8")
        self.recv_window = recv_window
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self._last_request_time = 0.0
        self._min_interval = 0.1

    def _route_base_url(self, path: str) -> str:
        """Route to correct base URL based on endpoint type."""
        if path in self.PAPI_ONLY_PATHS:
            return self.PAPI_BASE
        if path in self.FAPI_ONLY_PATHS:
            return self.FAPI_BASE
        # Default: try papi first for authenticated, fapi for public
        return self.PAPI_BASE

    def signed_request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        signed: bool = True,
    ) -> Any:
        """
        Make a request to the correct base URL with optional HMAC signing.

        Args:
            method: HTTP method (GET, POST, DELETE)
            path: Endpoint path (e.g., "/account", "/klines")
            params: Query parameters
            body: JSON body for POST requests
            signed: Whether to sign the request (False for public endpoints)
        """
        # Check if this endpoint should be mocked
        if path in self.MOCK_ENDPOINTS:
            logger.debug(f"Returning mock response for {path}")
            return self.MOCK_ENDPOINTS[path]

        # Rate limiting
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        base_url = self._route_base_url(path)
        params = dict(params or {})

        if signed:
            params["timestamp"] = str(int(time.time() * 1000))
            params["recvWindow"] = str(self.recv_window)
            query_string = urlencode(params)
            signature = hmac.new(
                self.secret_key, query_string.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            params["signature"] = signature

        url = f"{base_url}{path}"

        try:
            if method.upper() == "GET":
                resp = self.session.get(url, params=params, timeout=30)
            elif method.upper() == "POST":
                resp = self.session.post(url, params=params, json=body, timeout=30)
            elif method.upper() == "DELETE":
                resp = self.session.delete(url, params=params, timeout=30)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            self._last_request_time = time.time()
            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            logger.error(f"Request failed: {method} {path} status={status}: {e}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error: {method} {path}: {e}")
            raise

    def get_account(self) -> dict:
        """GET /account (papi, signed) — returns account equity, margin, status."""
        return self.signed_request("GET", "/account", signed=True)

    def get_position_risk(self, symbol: Optional[str] = None) -> list:
        """GET /positionRisk (papi, signed) — returns open positions."""
        params = {"symbol": symbol} if symbol else {}
        return self.signed_request("GET", "/positionRisk", params=params, signed=True)

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> list:
        """GET /klines (fapi, unsigned) — returns OHLCV candle data."""
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return self.signed_request("GET", "/klines", params=params, signed=False)

    def get_ticker_price(self, symbol: Optional[str] = None) -> Any:
        """GET /ticker/price (fapi, unsigned) — returns latest price(s)."""
        params = {"symbol": symbol} if symbol else {}
        return self.signed_request("GET", "/ticker/price", params=params, signed=False)

    def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        time_in_force: Optional[str] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
        new_client_order_id: Optional[str] = None,
    ) -> dict:
        """POST /order (papi, signed) — place a new order.
        Binance Portfolio Margin API requires positionSide=BOTH for
        accounts with hedge mode disabled (the default for most papi accounts)."""
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity,
            "positionSide": "BOTH",
        }
        if price is not None:
            params["price"] = price
        if time_in_force:
            params["timeInForce"] = time_in_force
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if reduce_only:
            params["reduceOnly"] = "true"
        if new_client_order_id:
            params["newClientOrderId"] = new_client_order_id
        return self.signed_request("POST", "/order", params=params, signed=True)

    def cancel_order(
        self,
        symbol: str,
        order_id: Optional[int] = None,
        orig_client_order_id: Optional[str] = None,
    ) -> dict:
        """DELETE /order (papi, signed) — cancel an open order."""
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        return self.signed_request("DELETE", "/order", params=params, signed=True)

    def get_order(
        self,
        symbol: str,
        order_id: Optional[int] = None,
        orig_client_order_id: Optional[str] = None,
    ) -> dict:
        """GET /order (papi, signed) — query order status."""
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        return self.signed_request("GET", "/order", params=params, signed=True)

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        """GET /openOrders (papi, signed) — list open orders."""
        params = {"symbol": symbol} if symbol else {}
        return self.signed_request("GET", "/openOrders", params=params, signed=True)

    def ping(self) -> dict:
        """GET /ping (fapi, unsigned) — test connectivity."""
        resp = self.session.get(f"{self.FAPI_BASE}/ping", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def close(self):
        """Close the underlying HTTP session."""
        self.session.close()