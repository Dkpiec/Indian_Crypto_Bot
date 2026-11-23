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

# Binance error codes that are transient / rate-limit related and worth a retry.
RETRYABLE_STATUS = {429, 418, 500, 502, 503, 504}


def _mask_signature(query_string: str) -> str:
    """Remove signature/timestamp from a signed query string for logging.

    The signed URL must never be written to logs verbatim: it carries the
    HMAC signature and API key context, which are secrets.
    """
    import re
    # Drop the signature value but keep a placeholder so param presence is visible
    masked = re.sub(r"(signature=)[^&]*", r"\1<redacted>", query_string)
    return masked


def _binance_error_detail(resp: requests.Response) -> str:
    """Extract Binance's {code, msg} body from an error response when present."""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            code = payload.get("code")
            msg = payload.get("msg")
            if code is not None or msg is not None:
                return f" binance_code={code} binance_msg={msg!r}"
    except Exception:
        pass
    return ""


class PapiClient:
    """
    Dual-endpoint HTTP client for Binance.

    - Authenticated calls (account, positionRisk, order, openOrders) → papi
    - Public market data (klines, ticker/price) → fapi (no auth required)

    Signed requests sync their timestamp against the exchange server
    (see sync_server_time) to absorb host clock skew — the most common
    cause of 100% order rejection (-1021 timestamp outside recvWindow).
    """

    PAPI_BASE = "https://papi.binance.com/papi/v1"
    # USDT-M futures under portfolio margin use /papi/v1/um/* sub-path
    PAPI_UM_BASE = "https://papi.binance.com/papi/v1/um"
    FAPI_BASE = "https://fapi.binance.com/fapi/v1"

    # Endpoints routed to papi root (/papi/v1/*) — account-level queries
    PAPI_ROOT_PATHS = {
        "/account",
        "/time",
    }

    # Endpoints routed to papi UM sub-path (/papi/v1/um/*) — USDT-M futures trading
    PAPI_UM_PATHS = {
        "/order",
        "/openOrders",
        "/positionRisk",
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
    }

    def __init__(self, api_key: str, secret_key: str, recv_window: int = 30000):
        self.api_key = api_key
        self.secret_key = secret_key.encode("utf-8")
        self.recv_window = recv_window
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self._last_request_time = 0.0
        self._min_interval = 0.1
        # Offset between local clock and exchange server (ms), set by sync_server_time().
        # time_offset_ms is kept (not offset_ms) for compatibility with callers.
        self.time_offset_ms = 0
        self._server_time_warned = False

    # ── Time sync (clock skew protection) ──

    def sync_server_time(self, max_skew_ms: int = 25000) -> int:
        """Sync local timestamp offset against the exchange server.

        Host clock drift (NTP out, container host with wrong date, etc.) is
        the classic root cause of every signed order failing with 400
        (-1021 timestamp outside recvWindow). We fetch /time (unsigned, free)
        from fapi, compute the skew, and apply it to every signed request's
        timestamp so orders are stamped with *server* time, not host time.

        Returns the measured skew in milliseconds (server - local), positive
        meaning the host clock is behind. Logs a loud warning when the skew
        is large enough to matter.
        """
        try:
            local_ms = int(time.time() * 1000)
            resp = self.session.get(f"{self.FAPI_BASE}/time", timeout=10)
            resp.raise_for_status()
            server_ms = int(resp.json()["serverTime"])
            skew = server_ms - local_ms
            self.time_offset_ms = skew
            if abs(skew) > max_skew_ms:
                logger.warning(
                    f"CLOCK SKEW: exchange server is {skew:+d} ms vs host clock "
                    f"(server={server_ms}, local={local_ms}). Signed requests are "
                    f"now stamped with server time. Fix NTP on the host: date is off."
                )
            else:
                logger.debug(f"Server time synced, skew={skew:+d} ms")
            return skew
        except Exception as e:
            logger.warning(f"Could not sync exchange server time: {e}")
            self.time_offset_ms = 0
            return 0

    @property
    def offset_ms(self) -> int:
        return self.time_offset_ms

    def _now_ms(self) -> int:
        """Current time in ms, adjusted by the measured server offset."""
        return int(time.time() * 1000) + self.time_offset_ms

    def _route_base_url(self, path: str) -> str:
        """Route to correct base URL based on endpoint type."""
        if path in self.PAPI_UM_PATHS:
            return self.PAPI_UM_BASE
        if path in self.PAPI_ROOT_PATHS:
            return self.PAPI_BASE
        if path in self.FAPI_ONLY_PATHS:
            return self.FAPI_BASE
        # Default: papi root for unknown authenticated endpoints
        return self.PAPI_BASE

    def signed_request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        signed: bool = True,
        max_retries: int = 2,
    ) -> Any:
        """
        Make a request to the correct base URL with optional HMAC signing.

        Retries only transient failures (network errors, 429/418/5xx) with
        backoff. A 4xx from a signed POST is a *rejected order* — retrying it
        blindly can double-fill, so non-rate-limit 4xx is NOT retried.

        Args:
            method: HTTP method (GET, POST, DELETE)
            path: Endpoint path (e.g., "/account", "/klines")
            params: Query parameters
            body: JSON body for POST requests
            signed: Whether to sign the request (False for public endpoints)
            max_retries: Number of retries for transient failures
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
            params["timestamp"] = str(self._now_ms())
            params["recvWindow"] = str(self.recv_window)
            query_string = urlencode(params)
            signature = hmac.new(
                self.secret_key, query_string.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            params["signature"] = signature
            # Keep a copy of the signed query string for safe logging (mask signature)
            safe_qs = _mask_signature(query_string + "&" + urlencode({"signature": "***"})) if signed else ""

        url = f"{base_url}{path}"

        safe_qs = ""
        attempt = 0
        while True:
            attempt += 1
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

                if resp.ok:
                    return resp.json()

                status = resp.status_code
                detail = _binance_error_detail(resp)
                if signed and status == 400 and detail and ("-1021" in detail or "-2018" in detail):
                    # Timestamp or signature rejection — likely clock skew we
                    # haven't caught yet. Re-sync once and retry without
                    # re-signing the same timestamp.
                    if not self._server_time_warned and attempt == 1:
                        logger.warning(
                            "Timestamp/signature rejected (-1021/-2018); "
                            "re-syncing server time before retrying."
                        )
                        self._server_time_warned = True
                        self.sync_server_time()
                        params["timestamp"] = str(self._now_ms())
                        q2 = urlencode(params)
                        s2 = hmac.new(
                            self.secret_key, q2.encode("utf-8"), hashlib.sha256
                        ).hexdigest()
                        params["signature"] = s2
                        continue
                    logger.error(
                        f"Request failed: {method} {path} status={status}{detail} "
                        f"(params: {safe_qs if signed else params})"
                    )
                    resp.raise_for_status()

                # Decide retry on transient codes only
                if status in RETRYABLE_STATUS and attempt <= max_retries:
                    wait = 2 ** (attempt - 1)
                    logger.warning(
                        f"Transient {status} on {method} {path}{detail}; retrying in {wait}s "
                        f"(attempt {attempt}/{max_retries})"
                    )
                    time.sleep(wait)
                    continue

                logger.error(
                    f"Request failed: {method} {path} status={status}{detail} "
                    f"(params: {safe_qs if signed else params})"
                )
                resp.raise_for_status()

            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                detail = _binance_error_detail(e.response) if e.response is not None else ""
                if status in RETRYABLE_STATUS and attempt <= max_retries:
                    wait = 2 ** (attempt - 1)
                    logger.warning(
                        f"HTTP {status} transient on {method} {path}; retry in {wait}s "
                        f"(attempt {attempt}/{max_retries}){detail}"
                    )
                    time.sleep(wait)
                    continue
                logger.error(f"Request failed: {method} {path} status={status}{detail}")
                raise
            except requests.exceptions.RequestException as e:
                if attempt <= max_retries:
                    wait = 2 ** (attempt - 1)
                    logger.warning(
                        f"Network error on {method} {path}: {e}; retry in {wait}s "
                        f"(attempt {attempt}/{max_retries})"
                    )
                    time.sleep(wait)
                    continue
                logger.error(f"Request error: {method} {path}: {e}")
                raise

    def get_account(self) -> dict:
        """GET /account (papi, signed) — returns account equity, margin, status."""
        return self.signed_request("GET", "/account", signed=True)

    def get_position_risk(self, symbol: Optional[str] = None) -> list:
        """GET /um/positionRisk (papi UM sub-path, signed) — returns open USDT-M positions.
        Portfolio Margin accounts store positions under /papi/v1/um/*, not /papi/v1/*.
        Failures propagate (no silent empty list) so callers can distinguish
        'no positions' from 'auth broken / network down'."""
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
        accounts with hedge mode disabled (the default for most papi accounts).
        Always pass new_client_order_id so retries are idempotent."""
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

    def get_server_time(self) -> int:
        """GET /time (fapi, unsigned) — exchange server time in ms."""
        resp = self.session.get(f"{self.FAPI_BASE}/time", timeout=10)
        resp.raise_for_status()
        return int(resp.json()["serverTime"])

    def ping(self) -> dict:
        """GET /ping (fapi, unsigned) — test connectivity."""
        resp = self.session.get(f"{self.FAPI_BASE}/ping", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def close(self):
        """Close the underlying HTTP session."""
        self.session.close()
