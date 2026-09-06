"""
کلاینت سطح‌پایین برای API صرافی تبدیل (Tabdeal).
مستندات رسمی: https://docs.tabdeal.org

مکانیزم احراز هویت دقیقاً شبیه بایننس است:
    - هدر X-MBX-APIKEY = api_key
    - پارامترها + timestamp به صورت query-string درمی‌آیند و با HMAC-SHA256
      و کلید api_secret امضا می‌شوند؛ خروجی به عنوان پارامتر signature اضافه می‌شود.

نکته‌ی مهم که در بررسی مستندات فهمیدیم:
    API عمومی تبدیل هیچ endpoint ای برای کندل تاریخی (kline/OHLCV) ندارد -
    فقط order book (depth)، لیست معاملات اخیر (trades) و اطلاعات بازار دارد.
    به همین دلیل، این ماژول فقط برای «اجرای واقعی سفارش» و «گرفتن قیمت لحظه‌ای/
    موجودی» استفاده می‌شود؛ سیگنال و تحلیل همچنان از بایننس (signal_bot.py) می‌آید.

این فایل عمداً هیچ سفارشی را به‌صورت خودکار در main.py یا trading_bot*.py صدا
نمی‌زند - فقط توابع را در اختیار می‌گذارد تا از tabdeal_executor.py و
tabdeal_test_connection.py به‌صورت آگاهانه استفاده شوند.
"""
from __future__ import annotations
import hashlib
import hmac
import time
import urllib.parse
import requests

BASE_URL = "https://api1.tabdeal.org"


class TabdealAPIError(Exception):
    """خطای برگردانده‌شده توسط سرور تبدیل (شامل code و message)."""
    def __init__(self, code, message, http_status=None):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(f"[Tabdeal error {code}] {message}")


class TabdealClient:
    def __init__(self, api_key: str = "", api_secret: str = "", base_url: str = BASE_URL, timeout: int = 20):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.timeout = timeout

    # ------------------------------------------------------------------
    # امضا و درخواست پایه
    # ------------------------------------------------------------------
    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(params, doseq=True)
        return hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _headers(self, signed: bool) -> dict:
        if signed:
            if not self.api_key:
                raise ValueError("api_key تنظیم نشده - نمی‌توان درخواست امضاشده فرستاد.")
            return {"X-MBX-APIKEY": self.api_key}
        return {}

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = dict(params or {})
        if signed:
            if not self.api_secret:
                raise ValueError("api_secret تنظیم نشده - نمی‌توان درخواست امضاشده فرستاد.")
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)

        url = self.base_url + path
        headers = self._headers(signed)

        try:
            if method == "GET":
                resp = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            elif method == "POST":
                resp = requests.post(url, params=params, headers=headers, timeout=self.timeout)
            elif method == "DELETE":
                resp = requests.delete(url, params=params, headers=headers, timeout=self.timeout)
            else:
                raise ValueError(f"متد نامعتبر: {method}")
        except requests.RequestException as e:
            raise TabdealAPIError(code="NETWORK", message=str(e))

        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise TabdealAPIError(code="PARSE", message="پاسخ سرور JSON معتبر نبود.", http_status=resp.status_code)

        if isinstance(data, dict) and "code" in data and "msg" in data:
            raise TabdealAPIError(code=data["code"], message=data["msg"], http_status=resp.status_code)

        if not resp.ok:
            raise TabdealAPIError(code=resp.status_code, message=str(data), http_status=resp.status_code)

        return data

    # ------------------------------------------------------------------
    # Endpointهای عمومی (بدون نیاز به api_key)
    # ------------------------------------------------------------------
    def ping(self):
        """تست اتصال به سرور - بدون پارامتر."""
        return self._request("GET", "/api/v1/ping")

    def server_time(self):
        return self._request("GET", "/api/v1/time")

    def exchange_info(self, symbols: list[str] | None = None):
        params = {}
        if symbols:
            params["symbols"] = ",".join(symbols)
        return self._request("GET", "/api/v1/exchangeInfo", params)

    def depth(self, symbol: str, limit: int = 20):
        return self._request("GET", "/api/v1/depth", {"symbol": symbol, "limit": limit})

    def recent_trades(self, symbol: str, limit: int = 20):
        return self._request("GET", "/api/v1/trades", {"symbol": symbol, "limit": limit})

    def get_mid_price(self, symbol: str) -> float:
        """
        قیمت تقریبی لحظه‌ای بازار = میانگین بهترین bid و بهترین ask.
        چون تبدیل endpoint مستقیم ticker/price ندارد، از order book استفاده می‌کنیم.
        """
        book = self.depth(symbol, limit=5)
        best_bid = float(book["bids"][0][0])
        best_ask = float(book["asks"][0][0])
        return (best_bid + best_ask) / 2

    # ------------------------------------------------------------------
    # Endpointهای خصوصی (نیاز به api_key + api_secret)
    # ------------------------------------------------------------------
    def account(self):
        """اطلاعات حساب: موجودی‌ها، مجوزهای معامله/برداشت/واریز."""
        return self._request("GET", "/api/v1/account", signed=True)

    def get_asset_balance(self, asset: str) -> dict | None:
        acc = self.account()
        for b in acc.get("balances", []):
            if b["asset"].upper() == asset.upper():
                return b
        return None

    def new_order(self, symbol: str, side: str, order_type: str, quantity: str | float,
                  price: str | float | None = None, stop_price: str | float | None = None,
                  new_client_order_id: str | None = None):
        """
        side: "BUY" | "SELL"
        order_type: "MARKET" | "LIMIT" | "STOP_LOSS_LIMIT"
        """
        params = {"symbol": symbol, "side": side, "type": order_type, "quantity": quantity}
        if price is not None:
            params["price"] = price
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if new_client_order_id is not None:
            params["newClientOrderId"] = new_client_order_id
        return self._request("POST", "/api/v1/order", params, signed=True)

    def new_oco_order(self, symbol: str, side: str, quantity: str | float,
                       price: str | float, stop_price: str | float, stop_limit_price: str | float,
                       limit_client_order_id: str | None = None, stop_client_order_id: str | None = None):
        """
        سفارش OCO: یک سفارش limit (حد سود) + یک سفارش stop_loss_limit (حد ضرر)
        هم‌زمان ثبت می‌شوند؛ با اجرای هرکدام، دیگری خودکار لغو می‌شود.
        side باید جهت خروج از پوزیشن باشد (برای بستن یک لانگ: side="SELL").
        """
        params = {
            "symbol": symbol, "side": side, "quantity": quantity,
            "price": price, "stopPrice": stop_price, "stopLimitPrice": stop_limit_price,
        }
        if limit_client_order_id is not None:
            params["limitClientOrderId"] = limit_client_order_id
        if stop_client_order_id is not None:
            params["stopClientOrderId"] = stop_client_order_id
        return self._request("POST", "/api/v1/orderList/oco", params, signed=True)

    def get_order(self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None):
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        return self._request("GET", "/api/v1/order", params, signed=True)

    def cancel_order(self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None):
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if orig_client_order_id is not None:
            params["origClientOrderId"] = orig_client_order_id
        return self._request("DELETE", "/api/v1/order", params, signed=True)

    def open_orders(self, symbol: str | None = None):
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/api/v1/openOrders", params, signed=True)

    def cancel_open_orders(self, symbol: str):
        return self._request("DELETE", "/api/v1/openOrders", {"symbol": symbol}, signed=True)
