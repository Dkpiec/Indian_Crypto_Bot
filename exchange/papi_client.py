"""
Native Binance Portfolio Margin (papi) HTTP client.
Direct HTTPS calls to https://papi.binance.com/papi/v1/* with HMAC-SHA256 signing.
No CCXT dependency. No fapi fallback. No monkey-patching.
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
    """Low-level HTTP client for Binance papi endpoints."""

    BASE_URL = "https://papi.binance.com/papi/v1"

    # Endpoints that don't exist on papi — return mock responses instead of 404
    MOCK_ENDPOINTS = {
        "/positionSide/dual": {"dualSidePosition": False},
        "/multiAssetsMargin": {"multiAssetsMargin": False},
        "/leverageBracket": [],
        "/positionRisk": [],
    }

    def __init__(self, api_key: str, secret_key: str, recv_window: int = 30000):
        self.api_key = api_key
        self.secret_key = secret_key.encode("utf-8")
        self.recv_window = recv_window
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        # Adaptive rate limiter state
        self._last_request_time = 0.0
        self._min_interval = 0.1  # 100ms between requests (conservative)

    def signed_request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> Any:
        """
        Make a signed request to a papi endpoint.

        Args:
            method: HTTP method (GET, POST, DELETE)
            path: Endpoint path (e.g., "/account", "/order")
            params: Query parameters (auto-signed with timestamp + recvWindow)
            body: JSON body for POST requests

        Returns:
            Parsed JSON response or mock response for unsupported endpoints

        Raises:
            requests.HTTPError: On non-2xx responses (except mocked endpoints)
        """
        # Check if this endpoint should be mocked
        if path in self.MOCK_ENDPOINTS:
            logger.debug(f"Returning mock response for {path}")
            return self.MOCK_ENDPOINTS[path]

        # Rate limiting
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        params = dict(params or {})
        params["timestamp"] = str(int(time.time() * 1000))
        params["recvWindow"] = str(self.recv_window)

        # Build query string and sign
        query_string = urlencode(params)
        signature = hmac.new(
            self.secret_key, query_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature

        url = f"{self.BASE_URL}{path}"

        try:
            if method.upper() == "GET":
                resp = self.session.get(url, params=params, timeout=30)
            elif method.upper() == "POST":
                resp = self.session.post(
                    url, params=params, json=body, timeout=30
                )
            elif method.upper() == "DELETE":
                resp = self.session.delete(url, params=params, timeout=30)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            self._last_request_time = time.time()
            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            logger.error(f"Papi request failed: {method} {path} status={status}: {e}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Papi request error: {method} {path}: {e}")
            raise

    def get_account(self) -> dict:
        """GET /account — returns account equity, margin, status."""
        return self.signed_request("GET", "/account")

    def get_position_risk(self, symbol: Optional[str] = None) -> list:
        """GET /positionRisk — returns open positions."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self.signed_request("GET", "/positionRisk", params=params)

    def get_ticker_price(self, symbol: Optional[str] = None) -> Any:
        """GET /ticker/price — returns latest price(s)."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self.signed_request("GET", "/ticker/price", params=params)

    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> list:
        """GET /klines — returns OHLCV candle data."""
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return self.signed_request("GET", "/klines", params=params)

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
        """POST /order — place a new order."""
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity,
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
        return self.signed_request("POST", "/order", params=params)

    def cancel_order(self, symbol: str, order_id: Optional[int] = None, orig_client_order_id: Optional[str] = None) -> dict:
        """DELETE /order — cancel an open order."""
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        return self.signed_request("DELETE", "/order", params=params)

    def get_order(self, symbol: str, order_id: Optional[int] = None, orig_client_order_id: Optional[str] = None) -> dict:
        """GET /order — query order status."""
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        return self.signed_request("GET", "/order", params=params)

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        """GET /openOrders — list open orders."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self.signed_request("GET", "/openOrders", params=params)

    def ping(self) -> dict:
        """GET /ping — test connectivity (unsigned)."""
        resp = self.session.get(f"{self.BASE_URL}/ping", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def close(self):
        """Close the underlying HTTP session."""
        self.session.close()