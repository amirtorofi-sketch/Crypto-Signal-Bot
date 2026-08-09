"""
شبیه‌ساز معامله‌گر خودکار برای استراتژی سوم (v2) - کاملاً مستقل از استراتژی‌های اول و دوم
موجودی، پوزیشن‌ها، و ربات تلگرام همگی جدا هستند.
"""

import os
import json
import time

from signal_bot import SYMBOLS, TIMEFRAME, KLINES_LIMIT, get_klines
from signal_bot_v2 import check_strategy_smc_v2, get_htf_bias_v2, SL_ATR_MULT, TP1_RR, TP2_RR, send_telegram_message_v2

FIXED_TRADE_AMOUNT = 15.0
STARTING_BALANCE = 1000.0

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions_v2.json")


def load_state() -> dict:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("balance", STARTING_BALANCE)
                data.setdefault("positions", {})
                data.setdefault("next_trade_id", 1)
                return data
        except Exception:
            pass
    return {"balance": STARTING_BALANCE, "positions": {}, "next_trade_id": 1}


def save_state(state: dict):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def open_notional_sum(state: dict) -> float:
    total = 0.0
    for pos in state["positions"].values():
        for lot_key in ("lot_a", "lot_b"):
            lot = pos[lot_key]
            if lot["status"] == "open":
                total += lot["qty"] * pos["entry_price"]
    return total


def open_position(state: dict, symbol: str, direction: str, entry_price: float, sl_price: float,
                   tp1_price: float, tp2_price: float, candle_time=None):
    if symbol in state["positions"]:
        existing_id = state["positions"][symbol].get("trade_id", "?")
        send_telegram_message_v2(
            f"⏭ سیگنال جدید برای <b>{symbol}</b> دریافت شد، ولی چون پوزیشن باز #{existing_id} داری، نادیده گرفته شد."
        )
        return

    qty_total = FIXED_TRADE_AMOUNT / entry_price if entry_price > 0 else 0
    notional = qty_total * entry_price
    if qty_total <= 0 or notional < 5:
        print(f"[{symbol}] حجم/ارزش نامعتبر، رد شد.")
        return

    qty_half = qty_total / 2
    trade_id = state.get("next_trade_id", 1)
    state["next_trade_id"] = trade_id + 1
    direction_label = "خرید (Long)" if direction == "long" else "فروش (Short)"

    state["positions"][symbol] = {
        "trade_id": trade_id, "symbol": symbol, "direction": direction,
        "candle_time": str(candle_time) if candle_time is not None else None,
        "entry_price": entry_price, "sl_price": sl_price,
        "tp1_price": tp1_price, "tp2_price": tp2_price,
        "qty_total": qty_total, "notional": notional,
        "lot_a": {"qty": qty_half, "target": "tp1", "status": "open"},
        "lot_b": {"qty": qty_total - qty_half, "target": "tp2", "status": "open"},
    }
    save_state(state)

    exposure = open_notional_sum(state)
    emoji = "🟢" if direction == "long" else "🔴"
    candle_line = f"زمان کندل: {candle_time}\n" if candle_time is not None else ""
    send_telegram_message_v2(
        f"{emoji} <b>#{trade_id} | پوزیشن فرضی {direction_label} باز شد (Paper Trading)</b> | ICT/SMC v2\n"
        f"نماد: <b>{symbol}</b>\n{candle_line}حجم: {qty_total:.6f}\nارزش ورودی: {notional:.2f}$\n"
        f"ورود: {entry_price:.6f}\nSL: {sl_price:.6f}\nTP1: {tp1_price:.6f}\nTP2: {tp2_price:.6f}\n"
        f"—\nموجودی نقدی: {state['balance']:.2f}$\nسرمایه‌ی درگیر: {exposure:.2f}$\n"
        f"ارزش کل حساب: {state['balance'] + exposure:.2f}$"
    )


def check_open_position(state: dict, symbol: str, pos: dict, last_high: float, last_low: float):
    changed = False
    is_long = pos.get("direction", "long") == "long"

    for lot_key in ("lot_a", "lot_b"):
        lot = pos[lot_key]
        if lot["status"] != "open":
            continue
        sl_price = pos["sl_price"]
        target_price = pos["tp1_price"] if lot["target"] == "tp1" else pos["tp2_price"]

        if is_long:
            hit_sl, hit_tp = last_low <= sl_price, last_high >= target_price
        else:
            hit_sl, hit_tp = last_high >= sl_price, last_low <= target_price

        if hit_sl:
            pnl = lot["qty"] * (sl_price - pos["entry_price"]) * (1 if is_long else -1)
            state["balance"] += pnl
            lot["status"] = "closed_sl"
            changed = True
            send_telegram_message_v2(f"🔴 <b>#{pos.get('trade_id','?')} | {lot_key}</b> برای <b>{symbol}</b> با حد ضرر بسته شد. (سود/ضرر: {pnl:+.2f}$)")
        elif hit_tp:
            pnl = lot["qty"] * (target_price - pos["entry_price"]) * (1 if is_long else -1)
            state["balance"] += pnl
            lot["status"] = "closed_tp"
            changed = True
            send_telegram_message_v2(f"🟢 <b>#{pos.get('trade_id','?')} | {lot_key}</b> برای <b>{symbol}</b> با حد سود بسته شد. (سود/ضرر: {pnl:+.2f}$)")
            if lot_key == "lot_a" and pos["lot_b"]["status"] == "open":
                pos["sl_price"] = pos["entry_price"]
                send_telegram_message_v2(f"🛡 <b>#{pos.get('trade_id','?')} | حد ضرر {symbol} به نقطه ورود منتقل شد (Risk-Free).</b>")

    if changed:
        if pos["lot_a"]["status"] != "open" and pos["lot_b"]["status"] != "open":
            del state["positions"][symbol]
            exposure = open_notional_sum(state)
            send_telegram_message_v2(
                f"✅ <b>#{pos.get('trade_id','?')}</b> | پوزیشن <b>{symbol}</b> کاملاً بسته شد.\n"
                f"موجودی نقدی: {state['balance']:.2f}$  |  سرمایه‌ی درگیر باقی‌مانده: {exposure:.2f}$"
            )
        save_state(state)


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
            print(f"[{symbol}] پوزیشن باز v2 موجود است -> بررسی SL/TP...")
            check_open_position(state, symbol, state["positions"][symbol], last_high, last_low)
            continue

        try:
            htf_bullish, htf_bearish = get_htf_bias_v2(symbol)
        except Exception:
            htf_bullish, htf_bearish = True, True

        res = check_strategy_smc_v2(df, htf_bullish, htf_bearish)
        if res is None:
            continue

        if res["buy"]:
            price = res["price"]; atr_v = res["atr"]
            sl = price - atr_v * SL_ATR_MULT
            risk = price - sl
            open_position(state, symbol, "long", price, sl, price + risk*TP1_RR, price + risk*TP2_RR, candle_time=res["candle_time"])
        elif res["sell"]:
            price = res["price"]; atr_v = res["atr"]
            sl = price + atr_v * SL_ATR_MULT
            risk = sl - price
            open_position(state, symbol, "short", price, sl, price - risk*TP1_RR, price - risk*TP2_RR, candle_time=res["candle_time"])
        else:
            print(f"[{symbol}] بدون سیگنال v2 (خرید={res['bull_score']}/7, فروش={res['bear_score']}/7)")

        time.sleep(0.3)

    save_state(state)


if __name__ == "__main__":
    main()
