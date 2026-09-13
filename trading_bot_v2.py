"""
شبیه‌ساز معامله‌گر خودکار برای استراتژی سوم (v2) - کاملاً مستقل از استراتژی‌های اول و دوم
موجودی، پوزیشن‌ها، و ربات تلگرام همگی جدا هستند.
"""

import os
import json
import time

from signal_bot import TIMEFRAME, KLINES_LIMIT, get_klines
from signal_bot import check_strategy_supertrend, ST_TP1_RR, ST_TP2_RR
from signal_bot_v2 import SYMBOLS
from signal_bot_v2 import check_strategy_smc_v2, get_htf_bias_v2, SL_ATR_MULT, TP1_RR, TP2_RR, send_telegram_message_v2

FIXED_TRADE_AMOUNT = 15.0
STARTING_BALANCE = 1000.0

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions_v2.json")

SOURCE_TAG = {"Supertrend+ADX": "ST", "ICT/SMC Scalp Pro v2": "SMC"}


def position_key(symbol: str, source: str) -> str:
    return f"{symbol}__{SOURCE_TAG.get(source, source)}"


def load_state() -> dict:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("balance", STARTING_BALANCE)
                data.setdefault("positions", {})
                data.setdefault("next_trade_id", 1)
                # مهاجرت: پوزیشن‌های قدیمی فقط با کلید symbol (قبل از اضافه‌شدن Supertrend)
                # به فرمت جدید symbol__SMC منتقل می‌شوند تا با پوزیشن‌های ST تداخل نکنند.
                migrated = {}
                for k, v in data["positions"].items():
                    if "__" not in k:
                        migrated[f"{k}__SMC"] = v
                        v.setdefault("source", "ICT/SMC Scalp Pro v2")
                    else:
                        migrated[k] = v
                data["positions"] = migrated
                return data
        except Exception:
            pass
    return {"balance": STARTING_BALANCE, "positions": {}, "next_trade_id": 1}


def save_state(state: dict):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def open_position(state: dict, symbol: str, direction: str, entry_price: float, sl_price: float,
                   tp1_price: float, tp2_price: float, source: str, candle_time=None):
    key = position_key(symbol, source)
    if key in state["positions"]:
        existing_id = state["positions"][key].get("trade_id", "?")
        send_telegram_message_v2(
            f"⏭ سیگنال جدید {source} برای <b>{symbol}</b> دریافت شد، ولی چون پوزیشن باز #{existing_id} "
            f"از همین استراتژی روی این نماد داری، نادیده گرفته شد."
        )
        return

    qty_total = FIXED_TRADE_AMOUNT / entry_price if entry_price > 0 else 0
    notional = qty_total * entry_price
    if qty_total <= 0 or notional < 5:
        print(f"[{symbol}] حجم/ارزش نامعتبر، رد شد.")
        return

    trade_id = state.get("next_trade_id", 1)
    state["next_trade_id"] = trade_id + 1
    direction_label = "خرید (Long)" if direction == "long" else "فروش (Short)"

    state["positions"][key] = {
        "trade_id": trade_id, "symbol": symbol, "source": source, "direction": direction,
        "candle_time": str(candle_time) if candle_time is not None else None,
        "entry_price": entry_price,
        "initial_sl_price": sl_price,
        "sl_price": sl_price,          # همیشه فقط یک SL فعال (نه دو تا)
        "tp1_price": tp1_price, "tp2_price": tp2_price,
        "qty_total": qty_total, "notional": notional,
        "qty_open": qty_total,          # حجم فعلاً باز (کل، تا قبل از TP1)
        "phase": "before_tp1",          # before_tp1 -> after_tp1
    }
    save_state(state)

    exposure = open_notional_sum(state)
    emoji = "🟢" if direction == "long" else "🔴"
    candle_line = f"زمان کندل: {candle_time}\n" if candle_time is not None else ""
    send_telegram_message_v2(
        f"{emoji} <b>#{trade_id} | پوزیشن فرضی {direction_label} باز شد (Paper Trading)</b> | {source}\n"
        f"نماد: <b>{symbol}</b>\n{candle_line}حجم: {qty_total:.6f}\nارزش ورودی: {notional:.2f}$\n"
        f"ورود: {entry_price:.6f}\nSL: {sl_price:.6f}\nTP1: {tp1_price:.6f}\nTP2: {tp2_price:.6f}\n"
        f"—\nموجودی نقدی: {state['balance']:.2f}$\nسرمایه‌ی درگیر: {exposure:.2f}$\n"
        f"ارزش کل حساب: {state['balance'] + exposure:.2f}$"
    )


def open_notional_sum(state: dict) -> float:
    total = 0.0
    for pos in state["positions"].values():
        total += pos["qty_open"] * pos["entry_price"]
    return total


def check_open_position(state: dict, key: str, pos: dict, last_high: float, last_low: float,
                         last_open: float, last_close: float):
    symbol = pos["symbol"]
    source = pos.get("source", "ICT/SMC Scalp Pro v2")
    is_long = pos.get("direction", "long") == "long"
    entry = pos["entry_price"]
    sl_price = pos["sl_price"]
    sign = 1 if is_long else -1
    candle_bullish = last_close >= last_open

    if pos["phase"] == "before_tp1":
        target_price = pos["tp1_price"]
        if is_long:
            hit_sl, hit_tp = last_low <= sl_price, last_high >= target_price
        else:
            hit_sl, hit_tp = last_high >= sl_price, last_low <= target_price

        # اگر هر دو در یک کندل برخورد کرده باشند، بر اساس جهت کندل تشخیص می‌دهیم کدام زودتر لمس شده
        # Long: کندل صعودی -> احتمالاً TP زودتر لمس شده | کندل نزولی -> احتمالاً SL زودتر لمس شده
        # Short: برعکس
        if hit_sl and hit_tp:
            if is_long:
                hit_tp, hit_sl = (True, False) if candle_bullish else (False, True)
            else:
                hit_tp, hit_sl = (True, False) if not candle_bullish else (False, True)

        if hit_sl:
            # کل پوزیشن یک‌جا با یک پیام بسته می‌شود (نه دوتا)
            pnl = pos["qty_open"] * (sl_price - entry) * sign
            state["balance"] += pnl
            del state["positions"][key]
            exposure = open_notional_sum(state)
            send_telegram_message_v2(
                f"🔴 <b>#{pos.get('trade_id','?')}</b> | پوزیشن <b>{symbol}</b> ({source}) با حد ضرر کامل بسته شد. (سود/ضرر: {pnl:+.2f}$)\n"
                f"موجودی نقدی: {state['balance']:.2f}$  |  سرمایه‌ی درگیر باقی‌مانده: {exposure:.2f}$"
            )
            save_state(state)

        elif hit_tp:
            qty_tp1 = pos["qty_total"] / 2
            pnl = qty_tp1 * (target_price - entry) * sign
            state["balance"] += pnl
            pos["qty_open"] = pos["qty_total"] - qty_tp1
            pos["phase"] = "after_tp1"
            pos["sl_price"] = entry  # انتقال حد ضرر به نقطه ورود (Risk-Free)
            save_state(state)
            send_telegram_message_v2(
                f"🟢 <b>#{pos.get('trade_id','?')}</b> | TP1 برای <b>{symbol}</b> ({source}) خورد. (سود/ضرر این بخش: {pnl:+.2f}$)\n"
                f"🛡 حد ضرر باقی‌مانده به نقطه ورود ({entry:.6f}) منتقل شد؛ منتظر TP2 می‌مانیم."
            )

    else:  # phase == "after_tp1"
        target_price = pos["tp2_price"]
        if is_long:
            hit_sl, hit_tp = last_low <= sl_price, last_high >= target_price
        else:
            hit_sl, hit_tp = last_high >= sl_price, last_low <= target_price

        if hit_sl and hit_tp:
            if is_long:
                hit_tp, hit_sl = (True, False) if candle_bullish else (False, True)
            else:
                hit_tp, hit_sl = (True, False) if not candle_bullish else (False, True)

        if hit_sl or hit_tp:
            exit_price = sl_price if hit_sl else target_price
            pnl = pos["qty_open"] * (exit_price - entry) * sign
            state["balance"] += pnl
            del state["positions"][key]
            exposure = open_notional_sum(state)
            label = "حد ضرر (Risk-Free)" if hit_sl else "حد سود TP2"
            send_telegram_message_v2(
                f"{'⚪' if hit_sl else '🟢'} <b>#{pos.get('trade_id','?')}</b> | پوزیشن <b>{symbol}</b> ({source}) با {label} کامل بسته شد. (سود/ضرر این بخش: {pnl:+.2f}$)\n"
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
        last_open = df["open"].iloc[-2]
        last_close = df["close"].iloc[-2]

        # --- استراتژی ۱: Supertrend+ADX (جهت عادی، بدون معکوس‌سازی) ---
        key_st = position_key(symbol, "Supertrend+ADX")
        if key_st in state["positions"]:
            print(f"[{symbol}] پوزیشن باز v2 (ST) موجود است -> بررسی SL/TP...")
            check_open_position(state, key_st, state["positions"][key_st], last_high, last_low, last_open, last_close)
        else:
            buy_st, sell_st, ct_st, price_st, st_line, adx_val = check_strategy_supertrend(df)
            if buy_st:
                risk = abs(price_st - st_line)
                open_position(state, symbol, "long", price_st, st_line,
                               price_st + risk * ST_TP1_RR, price_st + risk * ST_TP2_RR,
                               source="Supertrend+ADX", candle_time=ct_st)
            elif sell_st:
                risk = abs(st_line - price_st)
                open_position(state, symbol, "short", price_st, st_line,
                               price_st - risk * ST_TP1_RR, price_st - risk * ST_TP2_RR,
                               source="Supertrend+ADX", candle_time=ct_st)
            else:
                print(f"[{symbol}] بدون سیگنال ST جدید (ADX={adx_val:.1f})")

        # --- استراتژی ۲: ICT/SMC Scalp Pro v2 ---
        key_smc = position_key(symbol, "ICT/SMC Scalp Pro v2")
        if key_smc in state["positions"]:
            print(f"[{symbol}] پوزیشن باز v2 (SMC) موجود است -> بررسی SL/TP...")
            check_open_position(state, key_smc, state["positions"][key_smc], last_high, last_low, last_open, last_close)
        else:
            try:
                htf_bullish, htf_bearish = get_htf_bias_v2(symbol)
            except Exception:
                htf_bullish, htf_bearish = True, True

            res = check_strategy_smc_v2(df, htf_bullish, htf_bearish)
            if res is None:
                time.sleep(0.3)
                continue

            if res["buy"]:
                price = res["price"]; atr_v = res["atr"]
                sl = price - atr_v * SL_ATR_MULT
                risk = price - sl
                open_position(state, symbol, "long", price, sl, price + risk*TP1_RR, price + risk*TP2_RR,
                               source="ICT/SMC Scalp Pro v2", candle_time=res["candle_time"])
            elif res["sell"]:
                price = res["price"]; atr_v = res["atr"]
                sl = price + atr_v * SL_ATR_MULT
                risk = sl - price
                open_position(state, symbol, "short", price, sl, price - risk*TP1_RR, price - risk*TP2_RR,
                               source="ICT/SMC Scalp Pro v2", candle_time=res["candle_time"])
            else:
                print(f"[{symbol}] بدون سیگنال SMC v2 (خرید={res['bull_score']}/7, فروش={res['bear_score']}/7)")

        time.sleep(0.3)

    save_state(state)


if __name__ == "__main__":
    main()
