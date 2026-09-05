"""
ربات سیگنال خرید/فروش کریپتو -> تلگرام
بدون نیاز به API پولی TradingView. داده از API رایگان بایننس گرفته می‌شود.

دو استراتژی پیاده‌سازی شده (معادل پایتونیِ اندیکاتورهای Pine Script):
    1) Supertrend + ADX + EMA200
    2) ICT/SMC Scalp Pro - Confluence Score System (Order Block + FVG + Liquidity Sweep + ...)

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
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT",
    "CRVUSDT", "ROSEUSDT", "CHZUSDT", "ONEUSDT", "VETUSDT",
    "MASKUSDT", "MANAUSDT", "GALAUSDT",
]   # نمادهای مورد نظر (فرمت بایننس)
# نکته: KDAUSDT عمداً اضافه نشده چون Kadena (KDA) از بایننس دیلیست شده (اکتبر ۲۰۲۵)

TIMEFRAME = "15m"                              # تایم‌فریم اصلی
KLINES_LIMIT = 300                             # تعداد کندل تاریخی برای محاسبه اندیکاتورها

# --- استراتژی ۱: Supertrend + ADX ---
ATR_PERIOD = 10
ST_FACTOR = 3.5              # <-- از ۳.۰ به ۳.۵ افزایش یافت تا حد ضرر (خط Supertrend) بازتر بشه
ADX_LEN = 14
DI_LEN = 14
ADX_THRESHOLD = 15          # <-- طبق درخواست از ۲۰ به ۱۵ تغییر کرد
USE_VOL_FILTER_S1 = True
VOL_MA_LEN = 20
USE_EMA_FILTER_S1 = True
EMA_TREND_LEN = 200
ST_TP1_RR = 1.5             # نسبت ریسک/ریوارد هدف اول (بر پایه فاصله تا خط Supertrend)
ST_TP2_RR = 3.0             # نسبت ریسک/ریوارد هدف دوم

# --- استراتژی ۲: ICT/SMC Scalp Pro (Confluence Score System) ---
SWING_LEN = 3
OB_SEARCH_LOOKBACK = 15
OB_MAX_COUNT = 3
USE_DISPLACEMENT_FILTER = True
DISPLACEMENT_MULT = 1.3
ATR_LEN_S2 = 14
FVG_MAX_COUNT = 6
SWEEP_LOOKBACK = 8
VOL_SPIKE_MULT = 1.2
RSI_LEN = 14
MIN_SCORE = 5                # <-- طبق درخواست روی ۵ از ۷ تنظیم شد
USE_PREMIUM_DISCOUNT = True
USE_HTF_BIAS = True
HTF_TIMEFRAME = "1h"
HTF_EMA_LEN = 50
SL_ATR_MULT = 1.8           # <-- از ۱.۰ به ۱.۸ افزایش یافت تا کمتر با نویز بازار SL بخوریم
TP1_RR = 1.5
TP2_RR = 3.0

# --- تلگرام ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
# نکته: از data-api.binance.vision به‌جای api.binance.com استفاده می‌کنیم چون
# این یکی مخصوص داده‌ی عمومی بازاره و روی سرورهای GitHub Actions (که در آمریکا هستن)
# با خطای 451 (محدودیت جغرافیایی بایننس) مواجه نمی‌شه.
BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"


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


def find_pivots(values: np.ndarray, left: int, right: int, mode: str):
    n = len(values)
    out = [np.nan] * n
    for i in range(left, n - right):
        window = values[i - left:i + right + 1]
        if mode == "high":
            if values[i] == np.max(window):
                out[i] = values[i]
        else:
            if values[i] == np.min(window):
                out[i] = values[i]
    return out


def shift_confirm(arr, right: int):
    n = len(arr)
    out = [np.nan] * n
    for i in range(n):
        src = i - right
        if src >= 0:
            out[i] = arr[src]
    return out


# =====================================================================
# استراتژی ۱: Supertrend + ADX
# =====================================================================
def check_strategy_supertrend(df: pd.DataFrame):
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
    vol_ok = (df["volume"].iloc[i] > df["vol_ma"].iloc[i]) if USE_VOL_FILTER_S1 else True
    ema_up_ok = (df["close"].iloc[i] > df["ema_trend"].iloc[i]) if USE_EMA_FILTER_S1 else True
    ema_down_ok = (df["close"].iloc[i] < df["ema_trend"].iloc[i]) if USE_EMA_FILTER_S1 else True

    buy = bool(flip_bull and strong_trend and df["di_plus"].iloc[i] > df["di_minus"].iloc[i] and vol_ok and ema_up_ok)
    sell = bool(flip_bear and strong_trend and df["di_minus"].iloc[i] > df["di_plus"].iloc[i] and vol_ok and ema_down_ok)

    candle_time = df["open_time"].iloc[i]
    price = df["close"].iloc[i]
    st_line = st_val.iloc[i]
    adx_value = float(df["adx"].iloc[i])
    return buy, sell, candle_time, price, st_line, adx_value


# =====================================================================
# استراتژی ۲: ICT/SMC Scalp Pro - Confluence Score System
# =====================================================================
def get_htf_bias(symbol: str):
    df_htf = get_klines(symbol, HTF_TIMEFRAME, 100)
    close_htf = df_htf["close"]
    ema_htf = ema(close_htf, HTF_EMA_LEN)
    last_close = close_htf.iloc[-2]
    last_ema = ema_htf.iloc[-2]
    return bool(last_close > last_ema), bool(last_close < last_ema)


def check_strategy_smc(df: pd.DataFrame, htf_bullish: bool, htf_bearish: bool):
    df = df.copy().reset_index(drop=True)
    n = len(df)

    atr_series = atr(df, ATR_LEN_S2).values
    rsi_series = rsi(df["close"], RSI_LEN).values
    vol_ma_series = df["volume"].rolling(VOL_MA_LEN).mean().values

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    open_ = df["open"].values
    volume = df["volume"].values

    ph_raw = find_pivots(high, SWING_LEN, SWING_LEN, "high")
    pl_raw = find_pivots(low, SWING_LEN, SWING_LEN, "low")
    ph_confirmed = shift_confirm(ph_raw, SWING_LEN)
    pl_confirmed = shift_confirm(pl_raw, SWING_LEN)

    trend = 0
    structure_high = None
    structure_low = None
    last_swing_high = None
    last_swing_low = None
    last_bull_sweep_idx = None
    last_bear_sweep_idx = None
    bull_obs, bear_obs = [], []
    bull_fvgs, bear_fvgs = [], []

    target_i = n - 2
    result = None

    for j in range(n):
        prev_close = close[j - 1] if j > 0 else close[j]

        if not np.isnan(ph_confirmed[j]):
            last_swing_high = ph_confirmed[j]
        if not np.isnan(pl_confirmed[j]):
            last_swing_low = pl_confirmed[j]

        bullish_bos = bullish_choch = bearish_bos = bearish_choch = False
        if last_swing_high is not None and close[j] > last_swing_high and prev_close <= last_swing_high:
            if trend <= 0:
                bullish_choch = True
            else:
                bullish_bos = True
            trend = 1
            structure_high = last_swing_high
            structure_low = last_swing_low if last_swing_low is not None else structure_low
        if last_swing_low is not None and close[j] < last_swing_low and prev_close >= last_swing_low:
            if trend >= 0:
                bearish_choch = True
            else:
                bearish_bos = True
            trend = -1
            structure_low = last_swing_low
            structure_high = last_swing_high if last_swing_high is not None else structure_high

        bullish_sweep = last_swing_low is not None and low[j] < last_swing_low and close[j] > last_swing_low
        bearish_sweep = last_swing_high is not None and high[j] > last_swing_high and close[j] < last_swing_high
        if bullish_sweep:
            last_bull_sweep_idx = j
        if bearish_sweep:
            last_bear_sweep_idx = j
        recent_bull_sweep = last_bull_sweep_idx is not None and (j - last_bull_sweep_idx) <= SWEEP_LOOKBACK
        recent_bear_sweep = last_bear_sweep_idx is not None and (j - last_bear_sweep_idx) <= SWEEP_LOOKBACK

        bar_range = high[j] - low[j]
        cur_atr = atr_series[j] if not np.isnan(atr_series[j]) else 0
        displacement_ok = bar_range > cur_atr * DISPLACEMENT_MULT

        if (bullish_bos or bullish_choch) and (not USE_DISPLACEMENT_FILTER or displacement_ok):
            ob_idx = None
            for k in range(1, OB_SEARCH_LOOKBACK + 1):
                idx = j - k
                if idx < 0:
                    break
                if close[idx] < open_[idx]:
                    ob_idx = idx
                    break
            if ob_idx is not None:
                bull_obs.append({"top": high[ob_idx], "bottom": low[ob_idx]})
                if len(bull_obs) > OB_MAX_COUNT:
                    bull_obs.pop(0)

        if (bearish_bos or bearish_choch) and (not USE_DISPLACEMENT_FILTER or displacement_ok):
            ob_idx2 = None
            for k in range(1, OB_SEARCH_LOOKBACK + 1):
                idx = j - k
                if idx < 0:
                    break
                if close[idx] > open_[idx]:
                    ob_idx2 = idx
                    break
            if ob_idx2 is not None:
                bear_obs.append({"top": high[ob_idx2], "bottom": low[ob_idx2]})
                if len(bear_obs) > OB_MAX_COUNT:
                    bear_obs.pop(0)

        bull_obs = [ob for ob in bull_obs if close[j] >= ob["bottom"]]
        bear_obs = [ob for ob in bear_obs if close[j] <= ob["top"]]

        if j >= 2:
            if low[j] > high[j - 2]:
                bull_fvgs.append({"top": low[j], "bottom": high[j - 2]})
                if len(bull_fvgs) > FVG_MAX_COUNT:
                    bull_fvgs.pop(0)
            if high[j] < low[j - 2]:
                bear_fvgs.append({"top": low[j - 2], "bottom": high[j]})
                if len(bear_fvgs) > FVG_MAX_COUNT:
                    bear_fvgs.pop(0)

        bull_fvgs = [f for f in bull_fvgs if close[j] >= f["bottom"]]
        bear_fvgs = [f for f in bear_fvgs if close[j] <= f["top"]]

        bull_zone_tapped = False
        if trend == 1:
            for ob in bull_obs:
                if low[j] <= ob["top"] and low[j] >= ob["bottom"]:
                    bull_zone_tapped = True
            for f in bull_fvgs:
                if low[j] <= f["top"] and low[j] >= f["bottom"]:
                    bull_zone_tapped = True

        bear_zone_tapped = False
        if trend == -1:
            for ob in bear_obs:
                if high[j] >= ob["bottom"] and high[j] <= ob["top"]:
                    bear_zone_tapped = True
            for f in bear_fvgs:
                if high[j] >= f["bottom"] and high[j] <= f["top"]:
                    bear_zone_tapped = True

        if j == target_i:
            candle_range = high[j] - low[j]
            strong_bull_rej = close[j] > open_[j] and candle_range > 0 and (close[j] - low[j]) >= candle_range * 0.66
            strong_bear_rej = close[j] < open_[j] and candle_range > 0 and (high[j] - close[j]) >= candle_range * 0.66

            cur_rsi = rsi_series[j]
            prev_rsi = rsi_series[j - 1] if j > 0 else cur_rsi
            rsi_bull_ok = cur_rsi > 45 and cur_rsi > prev_rsi
            rsi_bear_ok = cur_rsi < 55 and cur_rsi < prev_rsi

            cur_vol_ma = vol_ma_series[j] if not np.isnan(vol_ma_series[j]) else 0
            vol_spike = (volume[j] > cur_vol_ma * VOL_SPIKE_MULT) if cur_vol_ma > 0 else False

            eq_level = (structure_high + structure_low) / 2 if (structure_high is not None and structure_low is not None) else None
            discount_zone = eq_level is not None and close[j] < eq_level
            premium_zone = eq_level is not None and close[j] > eq_level

            bull_score = (
                (1 if recent_bull_sweep else 0) + (1 if vol_spike else 0) + (1 if strong_bull_rej else 0)
                + (1 if (not USE_HTF_BIAS or htf_bullish) else 0)
                + (1 if (not USE_PREMIUM_DISCOUNT or discount_zone) else 0)
                + (1 if rsi_bull_ok else 0) + (1 if len(bull_fvgs) > 0 else 0)
            )
            bear_score = (
                (1 if recent_bear_sweep else 0) + (1 if vol_spike else 0) + (1 if strong_bear_rej else 0)
                + (1 if (not USE_HTF_BIAS or htf_bearish) else 0)
                + (1 if (not USE_PREMIUM_DISCOUNT or premium_zone) else 0)
                + (1 if rsi_bear_ok else 0) + (1 if len(bear_fvgs) > 0 else 0)
            )

            buy = bull_zone_tapped and bull_score >= MIN_SCORE
            sell = bear_zone_tapped and bear_score >= MIN_SCORE

            result = {
                "buy": buy, "sell": sell,
                "bull_score": bull_score, "bear_score": bear_score,
                "candle_time": df["open_time"].iloc[j], "price": close[j],
                "atr": cur_atr,
            }

    return result


# =====================================================================
# ارسال پیام تلگرام
# =====================================================================
def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("توکن یا چت‌آیدی تلگرام تنظیم نشده. پیام ارسال نشد:\n", text)
        return
    # پشتیبانی از چند گیرنده: چند Chat ID را با کاما از هم جدا کن
    chat_ids = [cid.strip() for cid in TELEGRAM_CHAT_ID.split(",") if cid.strip()]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in chat_ids:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        try:
            r = requests.post(url, data=payload, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"خطا در ارسال پیام تلگرام به {chat_id}:", e)


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

        # --- استراتژی ۱: Supertrend + ADX ---
        buy1, sell1, ct1, price1, _st_line1, _adx_val1 = check_strategy_supertrend(df)
        st_dbg = df.copy()
        st_val_dbg, st_dir_dbg = supertrend(st_dbg, ATR_PERIOD, ST_FACTOR)
        _, _, adx_dbg = adx_dmi(st_dbg, DI_LEN, ADX_LEN)
        print(f"[{symbol}] Supertrend+ADX -> ADX={adx_dbg.iloc[-2]:.1f} (آستانه={ADX_THRESHOLD}) | "
              f"جهت={'صعودی' if st_dir_dbg.iloc[-2] == -1 else 'نزولی'} | buy={buy1} sell={sell1}")
        key_buy1 = f"{symbol}_st_buy"
        key_sell1 = f"{symbol}_st_sell"

        if buy1 and state.get(key_buy1) != str(ct1):
            msg = (f"🟢 <b>سیگنال خرید</b> | استراتژی Supertrend+ADX\n"
                   f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\n"
                   f"قیمت: {price1:.6f}\nزمان کندل: {ct1}")
            send_telegram_message(msg)
            state[key_buy1] = str(ct1)

        if sell1 and state.get(key_sell1) != str(ct1):
            msg = (f"🔴 <b>سیگنال فروش</b> | استراتژی Supertrend+ADX\n"
                   f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\n"
                   f"قیمت: {price1:.6f}\nزمان کندل: {ct1}")
            send_telegram_message(msg)
            state[key_sell1] = str(ct1)

        # --- استراتژی ۲: ICT/SMC Scalp Pro ---
        try:
            htf_bullish, htf_bearish = get_htf_bias(symbol)
        except Exception as e:
            print(f"خطا در دریافت بایاس HTF برای {symbol}: {e}")
            htf_bullish, htf_bearish = True, True  # در صورت خطا، فیلتر HTF را خنثی می‌کند

        res = check_strategy_smc(df, htf_bullish, htf_bearish)
        if res is not None:
            print(f"[{symbol}] ICT/SMC -> امتیاز خرید={res['bull_score']}/7  امتیاز فروش={res['bear_score']}/7 "
                  f"(آستانه لازم={MIN_SCORE}) | buy={res['buy']} sell={res['sell']}")
            key_buy2 = f"{symbol}_smc_buy"
            key_sell2 = f"{symbol}_smc_sell"
            ct2 = res["candle_time"]
            price2 = res["price"]
            atr2 = res["atr"]

            if res["buy"] and state.get(key_buy2) != str(ct2):
                sl = price2 - atr2 * SL_ATR_MULT
                risk = price2 - sl
                tp1 = price2 + risk * TP1_RR
                tp2 = price2 + risk * TP2_RR
                msg = (f"🟢 <b>سیگنال خرید</b> | ICT/SMC Scalp Pro (امتیاز: {res['bull_score']}/7)\n"
                       f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\n"
                       f"قیمت: {price2:.6f}\nSL: {sl:.6f}\nTP1: {tp1:.6f}\nTP2: {tp2:.6f}\n"
                       f"زمان کندل: {ct2}")
                send_telegram_message(msg)
                state[key_buy2] = str(ct2)

            if res["sell"] and state.get(key_sell2) != str(ct2):
                sl = price2 + atr2 * SL_ATR_MULT
                risk = sl - price2
                tp1 = price2 - risk * TP1_RR
                tp2 = price2 - risk * TP2_RR
                msg = (f"🔴 <b>سیگنال فروش</b> | ICT/SMC Scalp Pro (امتیاز: {res['bear_score']}/7)\n"
                       f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\n"
                       f"قیمت: {price2:.6f}\nSL: {sl:.6f}\nTP1: {tp1:.6f}\nTP2: {tp2:.6f}\n"
                       f"زمان کندل: {ct2}")
                send_telegram_message(msg)
                state[key_sell2] = str(ct2)

        time.sleep(0.5)  # رعایت رِیت‌لیمیت API بایننس

    save_state(state)


if __name__ == "__main__":
    main()
