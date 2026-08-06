"""
شبیه‌ساز معامله‌گر خودکار (Paper Trading) - کاملاً محلی، بدون نیاز به هیچ صرافی یا حساب کاربری
"""

import os
import json
import time

from signal_bot import (
    SYMBOLS, TIMEFRAME, KLINES_LIMIT,
    get_klines, check_strategy_supertrend, check_strategy_smc, get_htf_bias,
    SL_ATR_MULT, TP1_RR, TP2_RR, ATR_PERIOD, ST_TP1_RR, ST_TP2_RR,
    send_telegram_message,
)
from signal_bot import atr as calc_atr

# =====================================================================
# تنظیمات
# =====================================================================
RISK_PER_TRADE = 0.10          # (دیگر استفاده نمی‌شود؛ برای مرجع نگه داشته شده)
FIXED_TRADE_AMOUNT = 15.0      # مبلغ ثابت ورودی به هر معامله (دلار مجازی)
STARTING_BALANCE = 1000.0      # موجودی فرضی اولیه (دلار مجازی)

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")


# =====================================================================
# مدیریت وضعیت (موجودی فرضی + پوزیشن‌های باز)
# =====================================================================
def load_state() -> dict:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "balance" not in data:
                    data["balance"] = STARTING_BALANCE
                if "positions" not in data:
                    data["positions"] = {}
                return data
        except Exception:
            pass
    return {"balance": STARTING_BALANCE, "positions": {}}


def save_state(state: dict):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# =====================================================================
# محاسبه حجم معامله بر پایه مبلغ ثابت ورودی
# =====================================================================
def calculate_position_size(entry_price: float, sl_price: float, balance: float):
    if entry_price <= 0:
        return 0.0
    return FIXED_TRADE_AMOUNT / entry_price


# =====================================================================
# باز کردن پوزیشن فرضی جدید (Long یا Short)
# =====================================================================
def open_position(state: dict, symbol: str, direction: str, entry_price: float, sl_price: float,
                  tp1_price: float, tp2_price: float, source: str):
    qty_total = calculate_position_size(entry_price, sl_price, state["balance"])
    notional = qty_total * entry_price

    if qty_total <= 0 or notional < 5:
        print(f"[{symbol}] حجم/ارزش معامله نامعتبر (qty={qty_total:.6f}, notional={notional:.2f}), رد شد.")
        return

    qty_half = qty_total / 2
    direction_label = "خرید (Long)" if direction == "long" else "فروش (Short)"

    state["positions"][symbol] = {
        "direction": direction,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "qty_total": qty_total,
        "notional": notional,
        "lot_a": {"qty": qty_half, "target": "tp1", "status": "open"},
        "lot_b": {"qty": qty_total - qty_half, "target": "tp2", "status": "open"},
        "source": source,
    }
    save_state(state)

    emoji = "🟢" if direction == "long" else "🔴"
    send_telegram_message(
        f"{emoji} <b>پوزیشن فرضی {direction_label} باز شد (Paper Trading)</b> | {source}\n"
        f"نماد: <b>{symbol}</b>\nحجم: {qty_total:.6f}\nارزش ورودی: {notional:.2f}$\n"
        f"ورود: {entry_price:.6f}\nSL: {sl_price:.6f}\nTP1: {tp1_price:.6f}\nTP2: {tp2_price:.6f}\n"
        f"موجودی فعلی: {state['balance']:.2f}$"
    )


# =====================================================================
# بررسی پوزیشن باز نسبت به کندل تازه بسته‌شده (شبیه‌سازی اصابت SL/TP)
# =====================================================================
def check_open_position(state: dict, symbol: str, pos: dict, last_high: float, last_low: float):
    changed = False
    sl_price = pos["sl_price"]
    direction = pos.get("direction", "long")
    is_long = direction == "long"

    for lot_key in ("lot_a", "lot_b"):
        lot = pos[lot_key]
        if lot["status"] != "open":
            continue

        target_price = pos["tp1_price"] if lot["target"] == "tp1" else pos["tp2_price"]

        if is_long:
            hit_sl = last_low <= sl_price
            hit_tp = last_high >= target_price
        else:
            hit_sl = last_high >= sl_price
            hit_tp = last_low <= target_price

        if hit_sl:
            exit_price = sl_price
            pnl = lot["qty"] * (exit_price - pos["entry_price"]) * (1 if is_long else -1)
            state["balance"] += pnl
            lot["status"] = "closed_sl"
            changed = True
            send_telegram_message(f"🔴 <b>{lot_key}</b> برای <b>{symbol}</b> با حد ضرر بسته شد. (سود/ضرر: {pnl:+.2f}$)")
        elif hit_tp:
            exit_price = target_price
            pnl = lot["qty"] * (exit_price - pos["entry_price"]) * (1 if is_long else -1)
            state["balance"] += pnl
            lot["status"] = "closed_tp"
            changed = True
            send_telegram_message(f"🟢 <b>{lot_key}</b> برای <b>{symbol}</b> با حد سود بسته شد. (سود/ضرر: {pnl:+.2f}$)")

            if lot_key == "lot_a" and pos["lot_b"]["status"] == "open":
                pos["sl_price"] = pos["entry_price"]
                send_telegram_message(
                    f"🛡 <b>حد ضرر پوزیشن {symbol} به نقطه ورود ({pos['entry_price']:.6f}) منتقل شد (Risk-Free).</b>"
                )

    if changed:
        if pos["lot_a"]["status"] != "open" and pos["lot_b"]["status"] != "open":
            del state["positions"][symbol]
            send_telegram_message(
                f"✅ پوزیشن <b>{symbol}</b> کاملاً بسته شد.\nموجودی فعلی: {state['balance']:.2f}$"
            )
        save_state(state)


# =====================================================================
# اجرای اصلی
# =====================================================================
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

        last_high = df["high"].iloc[-2]
        last_low = df["low"].iloc[-2]

        if symbol in state["positions"]:
            print(f"[{symbol}] پوزیشن باز موجود است -> بررسی وضعیت SL/TP...")
            check_open_position(state, symbol, state["positions"][symbol], last_high, last_low)
            continue

        atr_series = calc_atr(df, ATR_PERIOD)
        current_atr = atr_series.iloc[-2]

        buy1, sell1, _, price1, st_line = check_strategy_supertrend(df)
        if buy1:
            print(f"[{symbol}] سیگنال خرید Supertrend+ADX فعال شد -> تلاش برای باز کردن پوزیشن Long...")
            sl = st_line
            risk = abs(price1 - sl)
            tp1 = price1 + risk * ST_TP1_RR
            tp2 = price1 + risk * ST_TP2_RR
            open_position(state, symbol, "long", price1, sl, tp1, tp2, source="Supertrend+ADX")
            continue
        elif sell1:
            print(f"[{symbol}] سیگنال فروش Supertrend+ADX فعال شد -> تلاش برای باز کردن پوزیشن Short...")
            sl = st_line
            risk = abs(sl - price1)
            tp1 = price1 - risk * ST_TP1_RR
            tp2 = price1 - risk * ST_TP2_RR
            open_position(state, symbol, "short", price1, sl, tp1, tp2, source="Supertrend+ADX")
            continue

        try:
            htf_bullish, htf_bearish = get_htf_bias(symbol)
        except Exception:
            htf_bullish, htf_bearish = True, True

        res = check_strategy_smc(df, htf_bullish, htf_bearish)
        if res is not None and res["buy"]:
            print(f"[{symbol}] سیگنال خرید ICT/SMC فعال شد (امتیاز={res['bull_score']}/7) -> تلاش برای باز کردن پوزیشن Long...")
            price2 = res["price"]
            atr2 = res["atr"]
            sl = price2 - atr2 * SL_ATR_MULT
            risk = price2 - sl
            tp1 = price2 + risk * TP1_RR
            tp2 = price2 + risk * TP2_RR
            open_position(state, symbol, "long", price2, sl, tp1, tp2, source="ICT/SMC Scalp Pro")
        elif res is not None and res["sell"]:
            print(f"[{symbol}] سیگنال فروش ICT/SMC فعال شد (امتیاز={res['bear_score']}/7) -> تلاش برای باز کردن پوزیشن Short...")
            price2 = res["price"]
            atr2 = res["atr"]
            sl = price2 + atr2 * SL_ATR_MULT
            risk = sl - price2
            tp1 = price2 - risk * TP1_RR
            tp2 = price2 - risk * TP2_RR
            open_position(state, symbol, "short", price2, sl, tp1, tp2, source="ICT/SMC Scalp Pro")
        elif res is not None:
            print(f"[{symbol}] بدون پوزیشن باز، بدون سیگنال (امتیاز خرید={res['bull_score']}/7, امتیاز فروش={res['bear_score']}/7, Supertrend buy/sell=False)")
        else:
            print(f"[{symbol}] بدون پوزیشن باز، بدون سیگنال")

        time.sleep(0.3)

    save_state(state)


if __name__ == "__main__":
    main()
