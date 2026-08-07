"""
شبیه‌ساز معامله‌گر خودکار (Paper Trading) - کاملاً محلی، بدون نیاز به هیچ صرافی یا حساب کاربری

نکات مهم نسخه فعلی:
    - هر نماد به ازای هر استراتژی (Supertrend و ICT/SMC) جداگانه پوزیشن باز می‌کند؛
      یعنی دیگر یک استراتژی، استراتژی دیگر را روی همان نماد بلاک نمی‌کند.
    - بعد از هر سیگنال، دقیقاً مشخص می‌شود که پوزیشن باز شده یا (و به چه دلیل) رد شده.
    - در هر پیام، هم موجودی نقدی تحقق‌یافته و هم سرمایه‌ی درگیر در پوزیشن‌های باز نمایش داده می‌شود.
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
FIXED_TRADE_AMOUNT = 15.0      # مبلغ ثابت ورودی به هر معامله (دلار مجازی)
STARTING_BALANCE = 1000.0      # موجودی فرضی اولیه (دلار مجازی)

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")

SOURCE_TAG = {"Supertrend+ADX": "ST", "ICT/SMC Scalp Pro": "SMC"}


def position_key(symbol: str, source: str) -> str:
    """کلید پوزیشن = نماد + استراتژی، تا دو استراتژی روی یک نماد مزاحم هم نشوند."""
    return f"{symbol}__{SOURCE_TAG.get(source, source)}"


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
                if "next_trade_id" not in data:
                    data["next_trade_id"] = 1
                return data
        except Exception:
            pass
    return {"balance": STARTING_BALANCE, "positions": {}, "next_trade_id": 1}


def save_state(state: dict):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def open_notional_sum(state: dict) -> float:
    """مجموع سرمایه‌ای که الان در پوزیشن‌های باز (لات‌های هنوز باز) درگیر است."""
    total = 0.0
    for pos in state["positions"].values():
        for lot_key in ("lot_a", "lot_b"):
            lot = pos[lot_key]
            if lot["status"] == "open":
                total += lot["qty"] * pos["entry_price"]
    return total


# =====================================================================
# محاسبه حجم معامله بر پایه مبلغ ثابت ورودی
# =====================================================================
def calculate_position_size(entry_price: float):
    if entry_price <= 0:
        return 0.0
    return FIXED_TRADE_AMOUNT / entry_price


# =====================================================================
# باز کردن پوزیشن فرضی جدید (Long یا Short)
# =====================================================================
def open_position(state: dict, symbol: str, direction: str, entry_price: float, sl_price: float,
                  tp1_price: float, tp2_price: float, source: str, candle_time=None):
    key = position_key(symbol, source)

    if key in state["positions"]:
        existing_id = state["positions"][key].get("trade_id", "?")
        send_telegram_message(
            f"⏭ سیگنال جدید {source} برای <b>{symbol}</b> دریافت شد، ولی چون پوزیشن باز #{existing_id} "
            f"از همین استراتژی روی این نماد داری، نادیده گرفته شد (تا پوزیشن قبلی بسته شود)."
        )
        return

    qty_total = calculate_position_size(entry_price)
    notional = qty_total * entry_price

    if qty_total <= 0 or notional < 5:
        print(f"[{symbol}] حجم/ارزش معامله نامعتبر (qty={qty_total:.6f}, notional={notional:.2f}), رد شد.")
        return

    qty_half = qty_total / 2
    direction_label = "خرید (Long)" if direction == "long" else "فروش (Short)"

    trade_id = state.get("next_trade_id", 1)
    state["next_trade_id"] = trade_id + 1

    state["positions"][key] = {
        "trade_id": trade_id,
        "symbol": symbol,
        "source": source,
        "candle_time": str(candle_time) if candle_time is not None else None,
        "direction": direction,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "qty_total": qty_total,
        "notional": notional,
        "lot_a": {"qty": qty_half, "target": "tp1", "status": "open"},
        "lot_b": {"qty": qty_total - qty_half, "target": "tp2", "status": "open"},
    }
    save_state(state)

    exposure = open_notional_sum(state)
    emoji = "🟢" if direction == "long" else "🔴"
    candle_line = f"زمان کندل: {candle_time}\n" if candle_time is not None else ""
    send_telegram_message(
        f"{emoji} <b>#{trade_id} | پوزیشن فرضی {direction_label} باز شد (Paper Trading)</b> | {source}\n"
        f"نماد: <b>{symbol}</b>\n{candle_line}حجم: {qty_total:.6f}\nارزش ورودی: {notional:.2f}$\n"
        f"ورود: {entry_price:.6f}\nSL: {sl_price:.6f}\nTP1: {tp1_price:.6f}\nTP2: {tp2_price:.6f}\n"
        f"—\nموجودی نقدی (تحقق‌یافته): {state['balance']:.2f}$\n"
        f"سرمایه‌ی درگیر در پوزیشن‌های باز: {exposure:.2f}$\n"
        f"ارزش کل حساب (تقریبی): {state['balance'] + exposure:.2f}$"
    )


# =====================================================================
# بررسی پوزیشن باز نسبت به کندل تازه بسته‌شده (شبیه‌سازی اصابت SL/TP)
# =====================================================================
def check_open_position(state: dict, key: str, pos: dict, last_high: float, last_low: float):
    changed = False
    symbol = pos["symbol"]
    direction = pos.get("direction", "long")
    is_long = direction == "long"

    for lot_key in ("lot_a", "lot_b"):
        lot = pos[lot_key]
        if lot["status"] != "open":
            continue

        sl_price = pos["sl_price"]
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
            send_telegram_message(f"🔴 <b>#{pos.get('trade_id','?')} | {lot_key}</b> برای <b>{symbol}</b> ({pos['source']}) با حد ضرر بسته شد. (سود/ضرر: {pnl:+.2f}$)")
        elif hit_tp:
            exit_price = target_price
            pnl = lot["qty"] * (exit_price - pos["entry_price"]) * (1 if is_long else -1)
            state["balance"] += pnl
            lot["status"] = "closed_tp"
            changed = True
            send_telegram_message(f"🟢 <b>#{pos.get('trade_id','?')} | {lot_key}</b> برای <b>{symbol}</b> ({pos['source']}) با حد سود بسته شد. (سود/ضرر: {pnl:+.2f}$)")

            if lot_key == "lot_a" and pos["lot_b"]["status"] == "open":
                pos["sl_price"] = pos["entry_price"]
                send_telegram_message(
                    f"🛡 <b>#{pos.get('trade_id','?')} | حد ضرر پوزیشن {symbol} ({pos['source']}) به نقطه ورود ({pos['entry_price']:.6f}) منتقل شد (Risk-Free).</b>"
                )

    if changed:
        if pos["lot_a"]["status"] != "open" and pos["lot_b"]["status"] != "open":
            del state["positions"][key]
            exposure = open_notional_sum(state)
            send_telegram_message(
                f"✅ <b>#{pos.get('trade_id','?')}</b> | پوزیشن <b>{symbol}</b> ({pos['source']}) کاملاً بسته شد.\n"
                f"موجودی نقدی: {state['balance']:.2f}$  |  سرمایه‌ی درگیر باقی‌مانده: {exposure:.2f}$"
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

        # --- بررسی پوزیشن‌های باز موجود برای این نماد (هر استراتژی جدا) ---
        for src_name, tag in SOURCE_TAG.items():
            key = f"{symbol}__{tag}"
            if key in state["positions"]:
                print(f"[{key}] پوزیشن باز موجود است -> بررسی وضعیت SL/TP...")
                check_open_position(state, key, state["positions"][key], last_high, last_low)

        atr_series = calc_atr(df, ATR_PERIOD)
        current_atr = atr_series.iloc[-2]

        # --- استراتژی ۱: Supertrend + ADX ---
        buy1, sell1, ct1, price1, st_line = check_strategy_supertrend(df)
        if buy1:
            print(f"[{symbol}] سیگنال خرید Supertrend+ADX فعال شد -> تلاش برای باز کردن پوزیشن Long...")
            sl = st_line
            risk = abs(price1 - sl)
            tp1 = price1 + risk * ST_TP1_RR
            tp2 = price1 + risk * ST_TP2_RR
            open_position(state, symbol, "long", price1, sl, tp1, tp2, source="Supertrend+ADX", candle_time=ct1)
        elif sell1:
            print(f"[{symbol}] سیگنال فروش Supertrend+ADX فعال شد -> تلاش برای باز کردن پوزیشن Short...")
            sl = st_line
            risk = abs(sl - price1)
            tp1 = price1 - risk * ST_TP1_RR
            tp2 = price1 - risk * ST_TP2_RR
            open_position(state, symbol, "short", price1, sl, tp1, tp2, source="Supertrend+ADX", candle_time=ct1)

        # --- استراتژی ۲: ICT/SMC Scalp Pro ---
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
            open_position(state, symbol, "long", price2, sl, tp1, tp2, source="ICT/SMC Scalp Pro", candle_time=res["candle_time"])
        elif res is not None and res["sell"]:
            print(f"[{symbol}] سیگنال فروش ICT/SMC فعال شد (امتیاز={res['bear_score']}/7) -> تلاش برای باز کردن پوزیشن Short...")
            price2 = res["price"]
            atr2 = res["atr"]
            sl = price2 + atr2 * SL_ATR_MULT
            risk = sl - price2
            tp1 = price2 - risk * TP1_RR
            tp2 = price2 - risk * TP2_RR
            open_position(state, symbol, "short", price2, sl, tp1, tp2, source="ICT/SMC Scalp Pro", candle_time=res["candle_time"])
        elif res is not None:
            print(f"[{symbol}] بدون سیگنال SMC جدید (امتیاز خرید={res['bull_score']}/7, امتیاز فروش={res['bear_score']}/7)")

        time.sleep(0.3)

    save_state(state)


if __name__ == "__main__":
    main()
