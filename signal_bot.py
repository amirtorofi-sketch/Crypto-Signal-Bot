"""
ربات سیگنال خرید/فروش کریپتو -> تلگرام
بدون نیاز به API پولی TradingView. داده از API رایگان بایننس گرفته می‌شود.

دو استراتژی پیاده‌سازی شده (معادل پایتونیِ همان اندیکاتورهای Pine Script):
    1) EMA(9/21) Cross + RSI + MACD + Volume
    2) Supertrend + ADX + EMA200

نحوه اجرا:
    - محلی: python signal_bot.py
    - خودکار و رایگان: از طریق GitHub Actions (فایل .github/workflows/crypto_signals.yml)
"""

import os
import json
import time
import requests
import pandas as pd
import numpy as np

# =====================================================================
# تنظیمات قابل تغییر
# =====================================================================
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]   # نمادهای مورد نظر (فرمت بایننس)
TIMEFRAME = "15m"                              # تایم‌فریم (مطابق چیزی که روی TradingView استفاده می‌کردی)
KLINES_LIMIT = 300                             # تعداد کندل تاریخی برای محاسبه اندیکاتورها

# --- استراتژی ۱: EMA + RSI + MACD ---
EMA_FAST_LEN = 9
EMA_SLOW_LEN = 21
RSI_LEN = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOL_MA_LEN = 20
USE_VOL_FILTER_S1 = True

# --- استراتژی ۲: Supertrend + ADX ---
ATR_PERIOD = 10
ST_FACTOR = 3.0
ADX_LEN = 14
DI_LEN = 14
ADX_THRESHOLD = 20
USE_VOL_FILTER_S2 = True
USE_EMA_FILTER_S2 = True
EMA_TREND_LEN = 200

# --- تلگرام ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


# =====================================================================
# دریافت داده از بایننس (رایگان، بدون نیاز به API Key)
# =====================================================================
def get_klines(symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=20)
    resp.raise_for_status()
    raw = resp.json()
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df[["open_time", "open", "high", "low", "close", "volume"]]


# =====================================================================
# اندیکاتورهای پایه
# =====================================================================
def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def macd(series: pd.Series, fast: int, slow: int, signal: int):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def supertrend(df: pd.DataFrame, atr_len: int, factor: float):
    hl2 = (df["high"] + df["low"]) / 2
    atr_val = atr(df, atr_len)
    upperband = hl2 + factor * atr_val
    lowerband = hl2 - factor * atr_val

    final_upper = upperband.copy()
    final_lower = lowerband.copy()
    st = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)  # -1 = uptrend, 1 = downtrend

    for i in range(len(df)):
        if i == 0:
            final_upper.iloc[i] = upperband.iloc[i]
            final_lower.iloc[i] = lowerband.iloc[i]
            st.iloc[i] = final_upper.iloc[i]
            direction.iloc[i] = 1
            continue

        if upperband.iloc[i] < final_upper.iloc[i - 1] or df["close"].iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = upperband.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]

        if lowerband.iloc[i] > final_lower.iloc[i - 1] or df["close"].iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = lowerband.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]

        prev_st = st.iloc[i - 1]
        if prev_st == final_upper.iloc[i - 1]:
            if df["close"].iloc[i] <= final_upper.iloc[i]:
                st.iloc[i] = final_upper.iloc[i]
                direction.iloc[i] = 1
            else:
                st.iloc[i] = final_lower.iloc[i]
                direction.iloc[i] = -1
        else:
            if df["close"].iloc[i] >= final_lower.iloc[i]:
                st.iloc[i] = final_lower.iloc[i]
                direction.iloc[i] = -1
            else:
                st.iloc[i] = final_upper.iloc[i]
                direction.iloc[i] = 1

    return st, direction


def adx_dmi(df: pd.DataFrame, di_len: int, adx_len: int):
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr = pd.concat([
        (high - low),
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)

    tr_smooth = tr.ewm(alpha=1 / di_len, adjust=False).mean()
    plus_dm_smooth = plus_dm.ewm(alpha=1 / di_len, adjust=False).mean()
    minus_dm_smooth = minus_dm.ewm(alpha=1 / di_len, adjust=False).mean()

    di_plus = 100 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
    di_minus = 100 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))

    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1 / adx_len, adjust=False).mean()

    return di_plus.fillna(0), di_minus.fillna(0), adx_val.fillna(0)


# =====================================================================
# استراتژی ۱: EMA + RSI + MACD
# =====================================================================
def check_strategy_1(df: pd.DataFrame):
    df = df.copy()
    df["ema_fast"] = ema(df["close"], EMA_FAST_LEN)
    df["ema_slow"] = ema(df["close"], EMA_SLOW_LEN)
    df["rsi"] = rsi(df["close"], RSI_LEN)
    macd_line, signal_line = macd(df["close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    df["macd"] = macd_line
    df["macd_signal"] = signal_line
    df["vol_ma"] = df["volume"].rolling(VOL_MA_LEN).mean()

    i = -2  # آخرین کندل کاملاً بسته‌شده (آخرین ردیف = کندل درحال شکل‌گیری است)
    prev = i - 1

    ema_cross_up = df["ema_fast"].iloc[prev] <= df["ema_slow"].iloc[prev] and df["ema_fast"].iloc[i] > df["ema_slow"].iloc[i]
    ema_cross_down = df["ema_fast"].iloc[prev] >= df["ema_slow"].iloc[prev] and df["ema_fast"].iloc[i] < df["ema_slow"].iloc[i]

    macd_bull = df["macd"].iloc[i] > df["macd_signal"].iloc[i]
    macd_bear = df["macd"].iloc[i] < df["macd_signal"].iloc[i]

    rsi_val = df["rsi"].iloc[i]
    rsi_bull_ok = RSI_OVERSOLD < rsi_val < RSI_OVERBOUGHT and rsi_val > 40
    rsi_bear_ok = RSI_OVERSOLD < rsi_val < RSI_OVERBOUGHT and rsi_val < 60

    vol_ok = (df["volume"].iloc[i] > df["vol_ma"].iloc[i]) if USE_VOL_FILTER_S1 else True

    buy = bool(ema_cross_up and macd_bull and rsi_bull_ok and vol_ok)
    sell = bool(ema_cross_down and macd_bear and rsi_bear_ok and vol_ok)

    candle_time = df["open_time"].iloc[i]
    price = df["close"].iloc[i]
    return buy, sell, candle_time, price


# =====================================================================
# استراتژی ۲: Supertrend + ADX
# =====================================================================
def check_strategy_2(df: pd.DataFrame):
    df = df.copy()
    st_val, st_dir = supertrend(df, ATR_PERIOD, ST_FACTOR)
    df["st_dir"] = st_dir
    di_plus, di_minus, adx_val = adx_dmi(df, DI_LEN, ADX_LEN)
    df["di_plus"] = di_plus
    df["di_minus"] = di_minus
    df["adx"] = adx_val
    df["ema_trend"] = ema(df["close"], EMA_TREND_LEN)
    df["vol_ma"] = df["volume"].rolling(VOL_MA_LEN).mean()

    i = -2
    prev = i - 1

    flip_bull = df["st_dir"].iloc[prev] == 1 and df["st_dir"].iloc[i] == -1
    flip_bear = df["st_dir"].iloc[prev] == -1 and df["st_dir"].iloc[i] == 1

    strong_trend = df["adx"].iloc[i] > ADX_THRESHOLD
    vol_ok = (df["volume"].iloc[i] > df["vol_ma"].iloc[i]) if USE_VOL_FILTER_S2 else True
    ema_up_ok = (df["close"].iloc[i] > df["ema_trend"].iloc[i]) if USE_EMA_FILTER_S2 else True
    ema_down_ok = (df["close"].iloc[i] < df["ema_trend"].iloc[i]) if USE_EMA_FILTER_S2 else True

    buy = bool(flip_bull and strong_trend and df["di_plus"].iloc[i] > df["di_minus"].iloc[i] and vol_ok and ema_up_ok)
    sell = bool(flip_bear and strong_trend and df["di_minus"].iloc[i] > df["di_plus"].iloc[i] and vol_ok and ema_down_ok)

    candle_time = df["open_time"].iloc[i]
    price = df["close"].iloc[i]
    return buy, sell, candle_time, price


# =====================================================================
# ارسال پیام تلگرام
# =====================================================================
def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("توکن یا چت‌آیدی تلگرام تنظیم نشده. پیام ارسال نشد:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print("خطا در ارسال پیام تلگرام:", e)


# =====================================================================
# مدیریت وضعیت (جلوگیری از سیگنال تکراری)
# =====================================================================
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# =====================================================================
# اجرای اصلی
# =====================================================================
def main():
    state = load_state()

    for symbol in SYMBOLS:
        try:
            df = get_klines(symbol, TIMEFRAME, KLINES_LIMIT)
        except Exception as e:
            print(f"خطا در دریافت داده برای {symbol}: {e}")
            continue

        if len(df) < 210:
            print(f"داده کافی برای {symbol} نیست، رد شد.")
            continue

        # --- استراتژی ۱ ---
        buy1, sell1, candle_time1, price1 = check_strategy_1(df)
        key_buy1 = f"{symbol}_s1_buy"
        key_sell1 = f"{symbol}_s1_sell"

        if buy1 and state.get(key_buy1) != str(candle_time1):
            msg = (f"🟢 <b>سیگنال خرید</b> | استراتژی EMA+RSI+MACD\n"
                   f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\n"
                   f"قیمت: {price1:.4f}\nزمان کندل: {candle_time1}")
            send_telegram_message(msg)
            state[key_buy1] = str(candle_time1)

        if sell1 and state.get(key_sell1) != str(candle_time1):
            msg = (f"🔴 <b>سیگنال فروش</b> | استراتژی EMA+RSI+MACD\n"
                   f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\n"
                   f"قیمت: {price1:.4f}\nزمان کندل: {candle_time1}")
            send_telegram_message(msg)
            state[key_sell1] = str(candle_time1)

        # --- استراتژی ۲ ---
        buy2, sell2, candle_time2, price2 = check_strategy_2(df)
        key_buy2 = f"{symbol}_s2_buy"
        key_sell2 = f"{symbol}_s2_sell"

        if buy2 and state.get(key_buy2) != str(candle_time2):
            msg = (f"🟢 <b>سیگنال خرید</b> | استراتژی Supertrend+ADX\n"
                   f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\n"
                   f"قیمت: {price2:.4f}\nزمان کندل: {candle_time2}")
            send_telegram_message(msg)
            state[key_buy2] = str(candle_time2)

        if sell2 and state.get(key_sell2) != str(candle_time2):
            msg = (f"🔴 <b>سیگنال فروش</b> | استراتژی Supertrend+ADX\n"
                   f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\n"
                   f"قیمت: {price2:.4f}\nزمان کندل: {candle_time2}")
            send_telegram_message(msg)
            state[key_sell2] = str(candle_time2)

        time.sleep(0.5)  # رعایت رِیت‌لیمیت API بایننس

    save_state(state)


if __name__ == "__main__":
    main()
