"""
شبیه‌ساز معامله‌گر خودکار (Paper Trading) - کاملاً محلی، بدون نیاز به هیچ صرافی یا حساب کاربری

چون دسترسی به صرافی‌های بزرگ (بایننس و ...) برای برخی کاربران محدود است، این نسخه به‌جای
وصل‌شدن به یک صرافی واقعی، معاملات را به‌صورت کاملاً فرضی (Paper Trading) شبیه‌سازی می‌کند:
    - از همان داده‌ی رایگان و عمومی قیمت (بدون نیاز به حساب) استفاده می‌شود
    - یک موجودی مجازی (پیش‌فرض ۱۰۰۰ دلار) در فایل نگه‌داری می‌شود
    - ورود/خروج معاملات و برخورد به SL/TP با بررسی High/Low کندل‌های بسته‌شده شبیه‌سازی می‌شود
    - هیچ پول واقعی جابه‌جا نمی‌شود و به هیچ صرافی نیازی نیست

نکته درباره دقت شبیه‌سازی: چون فقط از High/Low کندل استفاده می‌کنیم (نه دنباله‌ی دقیق تیک‌به‌تیک
قیمت)، اگر در یک کندل هم به SL و هم به TP رسیده باشد، به‌صورت محافظه‌کارانه فرض می‌کنیم SL زودتر
اصابت کرده (رویکرد استاندارد در بک‌تست‌ها برای جلوگیری از خوش‌بینی کاذب).
"""

import os
import json
import time

from signal_bot import (
    SYMBOLS, TIMEFRAME, KLINES_LIMIT,
    get_klines, check_strategy_supertrend, check_strategy_smc, get_htf_bias,
    SL_ATR_MULT, TP1_RR, TP2_RR, ATR_PERIOD,
    send_telegram_message,
)
from signal_bot import atr as calc_atr

# =====================================================================
# تنظیمات
# =====================================================================
RISK_PER_TRADE = 0.10          # ۱۰٪ ریسک از سرمایه به‌ازای هر معامله (بر پایه فاصله تا SL)
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
# محاسبه حجم معامله بر پایه ریسک
# =====================================================================
def calculate_position_size(entry_price: float, sl_price: float, balance: float):
    risk_amount = balance * RISK_PER_TRADE
    sl_distance = entry_price - sl_price
    if sl_distance <= 0:
        return 0.0
    return risk_amount / sl_distance


# =====================================================================
# باز کردن پوزیشن فرضی جدید
# =====================================================================
def open_position(state: dict, symbol: str, entry_price: float, sl_price: float,
                   tp1_price: float, tp2_price: float, source: str):
    balance = state["balance"]
    qty_total = calculate_position_size(entry_price, sl_price, balance)
    cost = qty_total * entry_price

    if qty_total <= 0 or cost > balance or cost < 5:
        print(f"[{symbol}] حجم/هزینه معامله نامعتبر (qty={qty_total:.6f}, cost={cost:.2f}), رد شد.")
        return

    qty_half = qty_total / 2

    state["balance"] -= cost
    state["positions"][symbol] = {
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "qty_total": qty_total,
        "cost": cost,
        "lot_a": {"qty": qty_half, "target": "tp1", "status": "open"},
        "lot_b": {"qty": qty_total - qty_half, "target": "tp2", "status": "open"},
        "source": source,
    }
    save_state(state)

    send_telegram_message(
        f"🟢 <b>پوزیشن فرضی باز شد (Paper Trading)</b> | {source}\n"
        f"نماد: <b>{symbol}</b>\nحجم: {qty_total:.6f}\nهزینه: {cost:.2f}$\n"
        f"ورود: {entry_price:.6f}\nSL: {sl_price:.6f}\nTP1: {tp1_price:.6f}\nTP2: {tp2_price:.6f}\n"
        f"موجودی باقی‌مانده: {state['balance']:.2f}$"
    )


# =====================================================================
# بررسی پوزیشن باز نسبت به کندل تازه بسته‌شده (شبیه‌سازی اصابت SL/TP)
# =====================================================================
def check_open_position(state: dict, symbol: str, pos: dict, last_high: float, last_low: float):
    changed = False
    sl_price = pos["sl_price"]

    for lot_key in ("lot_a", "lot_b"):
        lot = pos[lot_key]
        if lot["status"] != "open":
            continue

        target_price = pos["tp1_price"] if lot["target"] == "tp1" else pos["tp2_price"]
        hit_sl = last_low <= sl_price
        hit_tp = last_high >= target_price

        if hit_sl:
            # محافظه‌کارانه: اگر هر دو در یک کندل برخورد کرده باشند، SL را ملاک می‌گیریم
            proceeds = lot["qty"] * sl_price
            state["balance"] += proceeds
            lot["status"] = "closed_sl"
            changed = True
            send_telegram_message(f"🔴 <b>{lot_key}</b> برای <b>{symbol}</b> با حد ضرر بسته شد. (+{proceeds:.2f}$)")
        elif hit_tp:
            proceeds = lot["qty"] * target_price
            state["balance"] += proceeds
            lot["status"] = "closed_tp"
            changed = True
            send_telegram_message(f"🟢 <b>{lot_key}</b> برای <b>{symbol}</b> با حد سود بسته شد. (+{proceeds:.2f}$)")

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
            check_open_position(state, symbol, state["positions"][symbol], last_high, last_low)
            continue

        atr_series = calc_atr(df, ATR_PERIOD)
        current_atr = atr_series.iloc[-2]

        # --- استراتژی ۱: Supertrend + ADX ---
        buy1, _, _, price1 = check_strategy_supertrend(df)
        if buy1:
            sl = price1 - current_atr * SL_ATR_MULT
            risk = price1 - sl
            tp1 = price1 + risk * TP1_RR
            tp2 = price1 + risk * TP2_RR
            open_position(state, symbol, price1, sl, tp1, tp2, source="Supertrend+ADX")
            continue

        # --- استراتژی ۲: ICT/SMC Scalp Pro ---
        try:
            htf_bullish, htf_bearish = get_htf_bias(symbol)
        except Exception:
            htf_bullish, htf_bearish = True, True

        res = check_strategy_smc(df, htf_bullish, htf_bearish)
        if res is not None and res["buy"]:
            price2 = res["price"]
            atr2 = res["atr"]
            sl = price2 - atr2 * SL_ATR_MULT
            risk = price2 - sl
            tp1 = price2 + risk * TP1_RR
            tp2 = price2 + risk * TP2_RR
            open_position(state, symbol, price2, sl, tp1, tp2, source="ICT/SMC Scalp Pro")

        time.sleep(0.3)

    save_state(state)


if __name__ == "__main__":
    main()
