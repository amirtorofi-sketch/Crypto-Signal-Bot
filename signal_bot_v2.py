"""
استراتژی سوم (مستقل) -> ربات تلگرام دوم
پورت پایتونی از "ICT/SMC Scalp Pro - Confluence Score System [Improved]"
تفاوت‌های کلیدی نسبت به استراتژی SMC اصلی:
    - Tap + Rejection دقیق (کندل قبلی بیرون زون، کندل الان تست کرده و رد شده)
    - فیلتر روند ADX>22 به‌جای شرط وجود FVG در امتیازدهی
    - Cooldown شش‌کندلی بین سیگنال‌های هم‌جهت
    - رد شدن سخت‌گیرانه‌تر (نسبی + مطلق بر پایه ATR)
"""

import os
import time
import numpy as np
import pandas as pd
import requests

from signal_bot import (
    get_klines, ema, rsi, atr, find_pivots, shift_confirm,
    TIMEFRAME, KLINES_LIMIT, HTF_TIMEFRAME, HTF_EMA_LEN,
)

# نمادهای مخصوص استراتژی سوم (v2) - کاملاً مستقل از لیست استراتژی اول و دوم
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT",
    "EURUSDT",   # یورو (Forex) - جفت‌ارز مستقیم روی بایننس
    "PAXGUSDT",  # طلا (Gold) - توکن PAX Gold، ۱ به ۱ به طلای واقعی گره خورده
]

# =====================================================================
# تنظیمات (دقیقاً طبق Pine Script جدید)
# =====================================================================
SWING_LEN = 3
OB_SEARCH_LOOKBACK = 15
OB_MAX_COUNT = 3
USE_DISPLACEMENT_FILTER = True
DISPLACEMENT_MULT = 1.3
ATR_LEN = 14
FVG_MAX_COUNT = 6
SWEEP_LOOKBACK = 8
VOL_MA_LEN = 20
VOL_SPIKE_MULT = 1.2
RSI_LEN = 14
MIN_SCORE = 3
USE_PREMIUM_DISCOUNT = True
USE_HTF_BIAS = True
USE_TREND_FILTER = True
ADX_TREND_THRESHOLD = 22
COOLDOWN_BARS = 6

SL_ATR_MULT = 2.0           # <-- طبق درخواست از ۱.۰ به ۲.۰ افزایش یافت
TP1_RR = 1.5
TP2_RR = 3.0

# --- تلگرام (ربات دوم، مستقل از ربات اول) ---
TELEGRAM_BOT_TOKEN_2 = os.environ.get("TELEGRAM_BOT_TOKEN_2", "")
TELEGRAM_CHAT_ID_2 = os.environ.get("TELEGRAM_CHAT_ID_2", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_v2.json")


def send_telegram_message_v2(text: str):
    if not TELEGRAM_BOT_TOKEN_2 or not TELEGRAM_CHAT_ID_2:
        print("توکن یا چت‌آیدی ربات دوم تنظیم نشده. پیام ارسال نشد:\n", text)
        return
    chat_ids = [c.strip() for c in TELEGRAM_CHAT_ID_2.split(",") if c.strip()]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN_2}/sendMessage"
    for chat_id in chat_ids:
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"خطا در ارسال پیام به ربات دوم ({chat_id}):", e)


def adx_dmi(df: pd.DataFrame, length: int = 14):
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    tr = pd.concat([(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    tr_s = tr.ewm(alpha=1/length, adjust=False).mean()
    pdm_s = plus_dm.ewm(alpha=1/length, adjust=False).mean()
    mdm_s = minus_dm.ewm(alpha=1/length, adjust=False).mean()
    di_plus = 100 * (pdm_s / tr_s.replace(0, np.nan))
    di_minus = 100 * (mdm_s / tr_s.replace(0, np.nan))
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx = dx.ewm(alpha=1/length, adjust=False).mean()
    return adx.fillna(0)


def get_htf_bias_v2(symbol: str):
    df_htf = get_klines(symbol, HTF_TIMEFRAME, 100)
    close_htf = df_htf["close"]
    ema_htf = ema(close_htf, HTF_EMA_LEN)
    return bool(close_htf.iloc[-2] > ema_htf.iloc[-2]), bool(close_htf.iloc[-2] < ema_htf.iloc[-2])


# =====================================================================
# استراتژی اصلی (پورت کامل از Pine Script)
# =====================================================================
def check_strategy_smc_v2(df: pd.DataFrame, htf_bullish: bool, htf_bearish: bool):
    df = df.copy().reset_index(drop=True)
    n = len(df)

    atr_series = atr(df, ATR_LEN).values
    rsi_series = rsi(df["close"], RSI_LEN).values
    adx_series = adx_dmi(df, 14).values
    vol_ma_series = df["volume"].rolling(VOL_MA_LEN).mean().values

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    open_ = df["open"].values
    volume = df["volume"].values

    ph_confirmed = shift_confirm(find_pivots(high, SWING_LEN, SWING_LEN, "high"), SWING_LEN)
    pl_confirmed = shift_confirm(find_pivots(low, SWING_LEN, SWING_LEN, "low"), SWING_LEN)

    trend = 0
    structure_high = structure_low = None
    last_swing_high = last_swing_low = None
    last_bull_sweep_idx = last_bear_sweep_idx = None
    bull_obs, bear_obs = [], []
    bull_fvgs, bear_fvgs = [], []
    last_long_signal_bar = last_short_signal_bar = None

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
            bullish_choch = trend <= 0
            bullish_bos = not bullish_choch
            trend = 1
            structure_high = last_swing_high
            structure_low = last_swing_low if last_swing_low is not None else structure_low
        if last_swing_low is not None and close[j] < last_swing_low and prev_close >= last_swing_low:
            bearish_choch = trend >= 0
            bearish_bos = not bearish_choch
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
            ob_idx = next((j - k for k in range(1, OB_SEARCH_LOOKBACK + 1) if j - k >= 0 and close[j - k] < open_[j - k]), None)
            if ob_idx is not None:
                bull_obs.append({"top": high[ob_idx], "bottom": low[ob_idx]})
                if len(bull_obs) > OB_MAX_COUNT:
                    bull_obs.pop(0)

        if (bearish_bos or bearish_choch) and (not USE_DISPLACEMENT_FILTER or displacement_ok):
            ob_idx2 = next((j - k for k in range(1, OB_SEARCH_LOOKBACK + 1) if j - k >= 0 and close[j - k] > open_[j - k]), None)
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

        # --- Tap + Rejection (نیازمند کندل قبلی) ---
        prev_low = low[j - 1] if j > 0 else low[j]
        prev_high = high[j - 1] if j > 0 else high[j]

        bull_zone_tapped = False
        if trend == 1:
            for ob in bull_obs:
                if prev_low > ob["top"] and low[j] <= ob["top"] and close[j] > ob["bottom"]:
                    bull_zone_tapped = True
            for f in bull_fvgs:
                if prev_low > f["top"] and low[j] <= f["top"] and close[j] > f["bottom"]:
                    bull_zone_tapped = True

        bear_zone_tapped = False
        if trend == -1:
            for ob in bear_obs:
                if prev_high < ob["bottom"] and high[j] >= ob["bottom"] and close[j] < ob["top"]:
                    bear_zone_tapped = True
            for f in bear_fvgs:
                if prev_high < f["bottom"] and high[j] >= f["bottom"] and close[j] < f["top"]:
                    bear_zone_tapped = True

        # --- امتیازدهی ---
        candle_range = high[j] - low[j]
        strong_bull_rej = (close[j] > open_[j] and candle_range > 0
                            and (close[j] - low[j]) >= candle_range * 0.66
                            and (close[j] - low[j]) > cur_atr * 0.3)
        strong_bear_rej = (close[j] < open_[j] and candle_range > 0
                            and (high[j] - close[j]) >= candle_range * 0.66
                            and (high[j] - close[j]) > cur_atr * 0.3)

        cur_rsi = rsi_series[j]
        prev_rsi = rsi_series[j - 1] if j > 0 else cur_rsi
        rsi_bull_ok = cur_rsi > 45 and cur_rsi > prev_rsi
        rsi_bear_ok = cur_rsi < 55 and cur_rsi < prev_rsi

        cur_vol_ma = vol_ma_series[j] if not np.isnan(vol_ma_series[j]) else 0
        vol_spike = (volume[j] > cur_vol_ma * VOL_SPIKE_MULT) if cur_vol_ma > 0 else False

        eq_level = (structure_high + structure_low) / 2 if (structure_high is not None and structure_low is not None) else None
        discount_zone = eq_level is not None and close[j] < eq_level
        premium_zone = eq_level is not None and close[j] > eq_level

        trend_strong = adx_series[j] > ADX_TREND_THRESHOLD

        bull_score = (
            (1 if recent_bull_sweep else 0) + (1 if vol_spike else 0) + (1 if strong_bull_rej else 0)
            + (1 if (USE_HTF_BIAS and htf_bullish) else 0)
            + (1 if (USE_PREMIUM_DISCOUNT and discount_zone) else 0)
            + (1 if rsi_bull_ok else 0)
            + (1 if (USE_TREND_FILTER and trend_strong) else 0)
        )
        bear_score = (
            (1 if recent_bear_sweep else 0) + (1 if vol_spike else 0) + (1 if strong_bear_rej else 0)
            + (1 if (USE_HTF_BIAS and htf_bearish) else 0)
            + (1 if (USE_PREMIUM_DISCOUNT and premium_zone) else 0)
            + (1 if rsi_bear_ok else 0)
            + (1 if (USE_TREND_FILTER and trend_strong) else 0)
        )

        long_cooldown_ok = last_long_signal_bar is None or (j - last_long_signal_bar) >= COOLDOWN_BARS
        short_cooldown_ok = last_short_signal_bar is None or (j - last_short_signal_bar) >= COOLDOWN_BARS

        buy = bull_zone_tapped and bull_score >= MIN_SCORE and long_cooldown_ok
        sell = bear_zone_tapped and bear_score >= MIN_SCORE and short_cooldown_ok

        if buy:
            last_long_signal_bar = j
        if sell:
            last_short_signal_bar = j

        if j == target_i:
            result = {
                "buy": buy, "sell": sell,
                "bull_score": bull_score, "bear_score": bear_score,
                "candle_time": df["open_time"].iloc[j], "price": close[j], "atr": cur_atr,
            }

    return result


# =====================================================================
# مدیریت وضعیت (فقط برای جلوگیری از سیگنال تکراری اطلاع‌رسانی)
# =====================================================================
import json


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    state = load_state()
    for symbol in SYMBOLS:
        try:
            df = get_klines(symbol, TIMEFRAME, KLINES_LIMIT)
        except Exception as e:
            print(f"[{symbol}] خطا در دریافت داده: {e}")
            continue
        if len(df) < 210:
            continue

        try:
            htf_bullish, htf_bearish = get_htf_bias_v2(symbol)
        except Exception:
            htf_bullish, htf_bearish = True, True

        res = check_strategy_smc_v2(df, htf_bullish, htf_bearish)
        if res is None:
            continue

        print(f"[{symbol}] SMC-v2 -> امتیاز خرید={res['bull_score']}/7 امتیاز فروش={res['bear_score']}/7 | buy={res['buy']} sell={res['sell']}")

        ct = res["candle_time"]
        price = res["price"]
        atr_v = res["atr"]

        key_buy = f"{symbol}_v2_buy"
        key_sell = f"{symbol}_v2_sell"

        if res["buy"] and state.get(key_buy) != str(ct):
            sl = price - atr_v * SL_ATR_MULT
            risk = price - sl
            tp1 = price + risk * TP1_RR
            tp2 = price + risk * TP2_RR
            msg = (f"🟢 <b>سیگنال خرید</b> | ICT/SMC Scalp Pro v2 (امتیاز: {res['bull_score']}/7)\n"
                   f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\nقیمت: {price:.6f}\n"
                   f"SL: {sl:.6f}\nTP1: {tp1:.6f}\nTP2: {tp2:.6f}\nزمان کندل: {ct}")
            send_telegram_message_v2(msg)
            state[key_buy] = str(ct)

        if res["sell"] and state.get(key_sell) != str(ct):
            sl = price + atr_v * SL_ATR_MULT
            risk = sl - price
            tp1 = price - risk * TP1_RR
            tp2 = price - risk * TP2_RR
            msg = (f"🔴 <b>سیگنال فروش</b> | ICT/SMC Scalp Pro v2 (امتیاز: {res['bear_score']}/7)\n"
                   f"نماد: <b>{symbol}</b>\nتایم‌فریم: {TIMEFRAME}\nقیمت: {price:.6f}\n"
                   f"SL: {sl:.6f}\nTP1: {tp1:.6f}\nTP2: {tp2:.6f}\nزمان کندل: {ct}")
            send_telegram_message_v2(msg)
            state[key_sell] = str(ct)

        time.sleep(0.3)

    save_state(state)


if __name__ == "__main__":
    main()
