"""
شبیه‌ساز معامله‌گر خودکار برای استراتژی سوم (v2) - کاملاً مستقل از استراتژی‌های اول و دوم
موجودی، پوزیشن‌ها، و ربات تلگرام همگی جدا هستند.
"""

import os
import csv
import json
import time
from datetime import datetime, timezone

from signal_bot import TIMEFRAME, KLINES_LIMIT, get_klines
from signal_bot_v2 import SYMBOLS
from signal_bot_v2 import check_strategy_smc_v2, get_htf_bias_v2, get_sl_atr_mult, TP1_RR, TP2_RR, send_telegram_message_v2

FIXED_TRADE_AMOUNT = 15.0
STARTING_BALANCE = 1000.0

# لوریج مخصوص هر نماد. نمادهایی که اینجا نیستن با لوریج پیش‌فرض (بدون اهرم) باز می‌شن.
LEVERAGE_OVERRIDES = {"ETHUSDT": 10.0}
DEFAULT_LEVERAGE = 1.0


def get_leverage(symbol: str) -> float:
    """لوریج مناسب برای هر نماد؛ اگه توی LEVERAGE_OVERRIDES نبود، پیش‌فرض ۱x برمی‌گرده."""
    return LEVERAGE_OVERRIDES.get(symbol, DEFAULT_LEVERAGE)


POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions_v2.json")
TRADES_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_log_v2.csv")

# همون ستون‌های trades_log.csv (استراتژی‌های ۱ و ۲) به‌علاوه‌ی signal_score/market_snapshot
# تا بشه بعداً تحلیل کرد که هر معامله با چه امتیاز/کانفلوئنسی و توی چه شرایطی از بازار باز شده.
TRADES_LOG_FIELDS = [
    "event_time_utc", "event_type", "trade_id", "symbol", "source", "timeframe", "session",
    "direction", "candle_time",
    "entry_price", "sl_price", "tp1_price", "tp2_price", "notional_usd", "leverage", "margin_usd",
    "signal_score", "market_snapshot",
    "exit_reason", "exit_price", "pnl", "balance_after",
]


def get_session(dt) -> str:
    """سشن معاملاتی بر پایه‌ی ساعت UTC (ساده‌شده: سه بازه‌ی ۸ ساعته) - عیناً مثل trading_bot.py."""
    try:
        if hasattr(dt, "hour"):
            h = dt.hour
        else:
            h = datetime.fromisoformat(str(dt)).hour
    except Exception:
        return ""
    if 0 <= h < 8:
        return "آسیا"
    elif 8 <= h < 16:
        return "لندن"
    else:
        return "نیویورک"


def log_trade_event(row: dict):
    """یک ردیف جدید به trades_log_v2.csv اضافه می‌کند (append-only، تاریخچه‌ی کامل و دائمی)."""
    file_exists = os.path.exists(TRADES_LOG_FILE)
    full_row = {field: row.get(field, "") for field in TRADES_LOG_FIELDS}
    try:
        with open(TRADES_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADES_LOG_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(full_row)
    except Exception as e:
        print(f"خطا در ثبت trades_log_v2.csv: {e}")


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


def open_position(state: dict, symbol: str, direction: str, entry_price: float, sl_price: float,
                   tp1_price: float, tp2_price: float, candle_time=None,
                   signal_score=None, market_snapshot=None):
    if symbol in state["positions"]:
        existing_id = state["positions"][symbol].get("trade_id", "?")
        send_telegram_message_v2(
            f"⏭ سیگنال جدید برای <b>{symbol}</b> دریافت شد، ولی چون پوزیشن باز #{existing_id} داری، نادیده گرفته شد."
        )
        return

    leverage = get_leverage(symbol)
    margin_usd = FIXED_TRADE_AMOUNT
    notional = margin_usd * leverage
    qty_total = notional / entry_price if entry_price > 0 else 0
    if qty_total <= 0 or notional < 5:
        print(f"[{symbol}] حجم/ارزش نامعتبر، رد شد.")
        return

    trade_id = state.get("next_trade_id", 1)
    state["next_trade_id"] = trade_id + 1
    direction_label = "خرید (Long)" if direction == "long" else "فروش (Short)"
    session = get_session(candle_time) if candle_time is not None else ""

    state["positions"][symbol] = {
        "trade_id": trade_id, "symbol": symbol, "direction": direction,
        "candle_time": str(candle_time) if candle_time is not None else None,
        "session": session,
        "entry_price": entry_price,
        "initial_sl_price": sl_price,
        "sl_price": sl_price,          # همیشه فقط یک SL فعال (نه دو تا)
        "tp1_price": tp1_price, "tp2_price": tp2_price,
        "qty_total": qty_total, "notional": notional,
        "leverage": leverage, "margin_usd": margin_usd,
        "signal_score": signal_score, "market_snapshot": market_snapshot,
        "qty_open": qty_total,          # حجم فعلاً باز (کل، تا قبل از TP1)
        "phase": "before_tp1",          # before_tp1 -> after_tp1
    }
    save_state(state)

    exposure = open_notional_sum(state)
    emoji = "🟢" if direction == "long" else "🔴"
    candle_line = f"زمان کندل: {candle_time}\n" if candle_time is not None else ""
    send_telegram_message_v2(
        f"{emoji} <b>#{trade_id} | پوزیشن فرضی {direction_label} باز شد (Paper Trading)</b> | ICT/SMC v2\n"
        f"نماد: <b>{symbol}</b>\n{candle_line}حجم: {qty_total:.6f}\nارزش معامله: {notional:.2f}$ (لوریج {leverage:g}x)\n"
        f"ورود: {entry_price:.6f}\nSL: {sl_price:.6f}\nTP1: {tp1_price:.6f}\nTP2: {tp2_price:.6f}\n"
        f"—\nموجودی نقدی: {state['balance']:.2f}$\nسرمایه‌ی درگیر: {exposure:.2f}$\n"
        f"ارزش کل حساب: {state['balance'] + exposure:.2f}$"
    )

    log_trade_event({
        "event_time_utc": datetime.now(timezone.utc).isoformat(),
        "event_type": "open", "trade_id": trade_id, "symbol": symbol, "source": "ICT/SMC Scalp Pro v2",
        "timeframe": TIMEFRAME, "session": session, "direction": direction,
        "candle_time": str(candle_time) if candle_time is not None else "",
        "entry_price": entry_price, "sl_price": sl_price, "tp1_price": tp1_price, "tp2_price": tp2_price,
        "notional_usd": round(notional, 4), "leverage": leverage, "margin_usd": round(margin_usd, 4),
        "signal_score": signal_score if signal_score is not None else "",
        "market_snapshot": json.dumps(market_snapshot, ensure_ascii=False) if market_snapshot is not None else "",
        "balance_after": round(state["balance"], 4),
    })


def open_notional_sum(state: dict) -> float:
    total = 0.0
    for pos in state["positions"].values():
        total += pos["qty_open"] * pos["entry_price"]
    return total


def _log_close(pos: dict, symbol: str, exit_reason: str, exit_price: float, pnl: float, balance_after: float):
    log_trade_event({
        "event_time_utc": datetime.now(timezone.utc).isoformat(),
        "event_type": "close", "trade_id": pos.get("trade_id", ""), "symbol": symbol,
        "source": "ICT/SMC Scalp Pro v2", "timeframe": TIMEFRAME, "session": pos.get("session", ""),
        "direction": pos.get("direction", ""), "candle_time": pos.get("candle_time", ""),
        "entry_price": pos.get("entry_price", ""), "sl_price": pos.get("sl_price", ""),
        "tp1_price": pos.get("tp1_price", ""), "tp2_price": pos.get("tp2_price", ""),
        "notional_usd": pos.get("notional", ""), "leverage": pos.get("leverage", ""),
        "margin_usd": pos.get("margin_usd", ""),
        "signal_score": pos.get("signal_score") if pos.get("signal_score") is not None else "",
        "market_snapshot": json.dumps(pos.get("market_snapshot"), ensure_ascii=False) if pos.get("market_snapshot") is not None else "",
        "exit_reason": exit_reason, "exit_price": exit_price, "pnl": round(pnl, 4),
        "balance_after": round(balance_after, 4),
    })


def check_open_position(state: dict, symbol: str, pos: dict, last_high: float, last_low: float,
                         last_open: float, last_close: float):
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
            send_telegram_message_v2(
                f"🔴 <b>#{pos.get('trade_id','?')}</b> | پوزیشن <b>{symbol}</b> با حد ضرر کامل بسته شد. (سود/ضرر: {pnl:+.2f}$)\n"
                f"موجودی نقدی: {state['balance']:.2f}$  |  سرمایه‌ی درگیر باقی‌مانده: {open_notional_sum(state) - pos['qty_open']*entry:.2f}$"
            )
            _log_close(pos, symbol, "sl_full", sl_price, pnl, state["balance"])
            del state["positions"][symbol]
            save_state(state)

        elif hit_tp:
            qty_tp1 = pos["qty_total"] / 2
            pnl = qty_tp1 * (target_price - entry) * sign
            state["balance"] += pnl
            _log_close(pos, symbol, "tp1_partial", target_price, pnl, state["balance"])
            pos["qty_open"] = pos["qty_total"] - qty_tp1
            pos["phase"] = "after_tp1"
            pos["sl_price"] = entry  # انتقال حد ضرر به نقطه ورود (Risk-Free)
            save_state(state)
            send_telegram_message_v2(
                f"🟢 <b>#{pos.get('trade_id','?')}</b> | TP1 برای <b>{symbol}</b> خورد. (سود/ضرر این بخش: {pnl:+.2f}$)\n"
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
            exposure = open_notional_sum(state) - pos["qty_open"] * entry
            label = "حد ضرر (Risk-Free)" if hit_sl else "حد سود TP2"
            send_telegram_message_v2(
                f"{'⚪' if hit_sl else '🟢'} <b>#{pos.get('trade_id','?')}</b> | پوزیشن <b>{symbol}</b> با {label} کامل بسته شد. (سود/ضرر این بخش: {pnl:+.2f}$)\n"
                f"موجودی نقدی: {state['balance']:.2f}$  |  سرمایه‌ی درگیر باقی‌مانده: {exposure:.2f}$"
            )
            _log_close(pos, symbol, "sl_be" if hit_sl else "tp2", exit_price, pnl, state["balance"])
            del state["positions"][symbol]
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

        if symbol in state["positions"]:
            print(f"[{symbol}] پوزیشن باز v2 موجود است -> بررسی SL/TP...")
            check_open_position(state, symbol, state["positions"][symbol], last_high, last_low, last_open, last_close)
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
            sl = price - atr_v * get_sl_atr_mult(symbol)
            risk = price - sl
            open_position(state, symbol, "long", price, sl, price + risk*TP1_RR, price + risk*TP2_RR,
                          candle_time=res["candle_time"], signal_score=res["bull_score"],
                          market_snapshot=res.get("confluence"))
        elif res["sell"]:
            price = res["price"]; atr_v = res["atr"]
            sl = price + atr_v * get_sl_atr_mult(symbol)
            risk = sl - price
            open_position(state, symbol, "short", price, sl, price - risk*TP1_RR, price - risk*TP2_RR,
                          candle_time=res["candle_time"], signal_score=res["bear_score"],
                          market_snapshot=res.get("confluence"))
        else:
            print(f"[{symbol}] بدون سیگنال v2 (خرید={res['bull_score']}/7, فروش={res['bear_score']}/7)")

        time.sleep(0.3)

    save_state(state)


if __name__ == "__main__":
    main()
