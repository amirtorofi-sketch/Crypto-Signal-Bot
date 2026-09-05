"""
شبیه‌ساز معامله‌گر خودکار (Paper Trading) - کاملاً محلی، بدون نیاز به هیچ صرافی یا حساب کاربری

نکات مهم نسخه فعلی:
    - هر نماد به ازای هر استراتژی (Supertrend و ICT/SMC) جداگانه پوزیشن باز می‌کند؛
      یعنی دیگر یک استراتژی، استراتژی دیگر را روی همان نماد بلاک نمی‌کند.
    - بعد از هر سیگنال، دقیقاً مشخص می‌شود که پوزیشن باز شده یا (و به چه دلیل) رد شده.
    - در هر پیام، هم موجودی نقدی تحقق‌یافته و هم سرمایه‌ی درگیر در پوزیشن‌های باز نمایش داده می‌شود.
"""

import os
import csv
import json
import time
from datetime import datetime, timezone

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
STARTING_BALANCE = 1000.0      # موجودی فرضی اولیه (دلار مجازی)

# فعلاً فقط Supertrend+ADX فعاله (به‌خاطر نیاز به داده‌ی بیشتر برای ICT/SMC خاموش شد)
ENABLE_SMC = False

# حجم هر معامله به تفکیک استراتژی (چون Win Rate بالای Supertrend+ADX توجیه‌کننده‌ی حجم بیشتره)
TRADE_AMOUNT_BY_SOURCE = {
    "Supertrend+ADX": 300.0,
    "ICT/SMC Scalp Pro": 100.0,
}
DEFAULT_TRADE_AMOUNT = 100.0

# لوریج به تفکیک استراتژی: مارجین واقعی کم‌شده از موجودی = حجم معامله / لوریج
# (سود/ضرر همچنان بر مبنای کل حجم معامله محاسبه می‌شود، چون خودِ لوریج یعنی همین)
LEVERAGE_BY_SOURCE = {
    "Supertrend+ADX": 3.0,
    "ICT/SMC Scalp Pro": 1.0,
}
DEFAULT_LEVERAGE = 1.0

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")
TRADES_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_log.csv")

SOURCE_TAG = {"Supertrend+ADX": "ST", "ICT/SMC Scalp Pro": "SMC"}

TRADES_LOG_FIELDS = [
    "event_time_utc", "event_type", "trade_id", "symbol", "source", "timeframe", "session",
    "raw_direction", "final_direction", "candle_time",
    "entry_price", "sl_price", "tp1_price", "tp2_price", "notional_usd", "leverage", "margin_usd",
    "adx_value", "lot", "exit_reason", "exit_price", "pnl", "balance_after",
]


def get_session(dt) -> str:
    """سشن معاملاتی بر پایه‌ی ساعت UTC (ساده‌شده: سه بازه‌ی ۸ ساعته)."""
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
    """یک ردیف جدید به trades_log.csv اضافه می‌کند (append-only، تاریخچه‌ی کامل و دائمی)."""
    file_exists = os.path.exists(TRADES_LOG_FILE)
    full_row = {field: row.get(field, "") for field in TRADES_LOG_FIELDS}
    try:
        with open(TRADES_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADES_LOG_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(full_row)
    except Exception as e:
        print(f"خطا در ثبت trades_log.csv: {e}")


def resolve_direction_and_levels(raw_direction: str, entry: float, raw_sl: float, rr1: float, rr2: float):
    """
    جهت سیگنال خام استراتژی همیشه معکوس اجرا می‌شود (تست فرضیه‌ی Bias منفی
    استراتژی). raw_sl سطحی است که خودِ استراتژی برای جهت خام (نه جهت نهایی)
    حساب کرده (خط Supertrend یا ورود ± ATR*ضریب)، و از آن فقط "فاصله‌ی ریسک"
    استخراج می‌شود؛ SL/TP نهایی با همان فاصله و به‌صورت آینه‌ای حول قیمت
    ورود، در سمت مخالف بازتعریف می‌شوند (چون خط خام برای جهت مخالف بی‌معنی است).
    """
    risk = abs(entry - raw_sl)
    if raw_direction == "long":
        # سیگنال خام Long بود -> حالا Short می‌گیریم
        return "short", entry + risk, entry - risk * rr1, entry - risk * rr2
    else:
        # سیگنال خام Short بود -> حالا Long می‌گیریم
        return "long", entry - risk, entry + risk * rr1, entry + risk * rr2


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


def open_margin_sum(state: dict) -> float:
    """مجموع مارجین واقعی (سرمایه‌ی نقدی درگیر) در لات‌های هنوز باز -- همون چیزی که باید در برابر موجودی نقدی محدود بشه."""
    total = 0.0
    for pos in state["positions"].values():
        leverage = pos.get("leverage", DEFAULT_LEVERAGE) or DEFAULT_LEVERAGE
        for lot_key in ("lot_a", "lot_b"):
            lot = pos[lot_key]
            if lot["status"] == "open":
                lot_notional = lot["qty"] * pos["entry_price"]
                total += lot_notional / leverage
    return total


def open_notional_sum(state: dict) -> float:
    """مجموع کل ارزش پوزیشن (حجم واقعی معامله، صرف‌نظر از لوریج) در لات‌های باز."""
    total = 0.0
    for pos in state["positions"].values():
        for lot_key in ("lot_a", "lot_b"):
            lot = pos[lot_key]
            if lot["status"] == "open":
                total += lot["qty"] * pos["entry_price"]
    return total


# =====================================================================
# محاسبه حجم معامله بر پایه مبلغ ثابت ورودی (به تفکیک استراتژی)
# =====================================================================
def calculate_position_size(entry_price: float, source: str):
    if entry_price <= 0:
        return 0.0
    amount = TRADE_AMOUNT_BY_SOURCE.get(source, DEFAULT_TRADE_AMOUNT)
    return amount / entry_price


# =====================================================================
# باز کردن پوزیشن فرضی جدید (Long یا Short)
# =====================================================================
def open_position(state: dict, symbol: str, direction: str, entry_price: float, sl_price: float,
                  tp1_price: float, tp2_price: float, source: str, candle_time=None,
                  raw_direction: str = "", adx_value=None):
    key = position_key(symbol, source)

    if key in state["positions"]:
        existing_id = state["positions"][key].get("trade_id", "?")
        send_telegram_message(
            f"⏭ سیگنال جدید {source} برای <b>{symbol}</b> دریافت شد، ولی چون پوزیشن باز #{existing_id} "
            f"از همین استراتژی روی این نماد داری، نادیده گرفته شد (تا پوزیشن قبلی بسته شود)."
        )
        return

    qty_total = calculate_position_size(entry_price, source)
    notional = qty_total * entry_price

    if qty_total <= 0 or notional < 5:
        print(f"[{symbol}] حجم/ارزش معامله نامعتبر (qty={qty_total:.6f}, notional={notional:.2f}), رد شد.")
        return

    leverage = LEVERAGE_BY_SOURCE.get(source, DEFAULT_LEVERAGE) or DEFAULT_LEVERAGE
    margin_needed = notional / leverage

    used_margin = open_margin_sum(state)
    free_margin = state["balance"] - used_margin
    if margin_needed > free_margin:
        print(f"[{symbol}] سرمایه‌ی آزاد کافی نیست (نیاز: {margin_needed:.2f}$, آزاد: {free_margin:.2f}$), سیگنال رد شد.")
        send_telegram_message(
            f"⛔ سیگنال {source} برای <b>{symbol}</b> رد شد: سرمایه‌ی آزاد کافی نیست.\n"
            f"مارجین موردنیاز: {margin_needed:.2f}$  |  مارجین آزاد: {free_margin:.2f}$\n"
            f"(موجودی نقدی: {state['balance']:.2f}$  |  مارجین درگیر: {used_margin:.2f}$)"
        )
        log_trade_event({
            "event_time_utc": datetime.now(timezone.utc).isoformat(),
            "event_type": "signal_skipped_no_margin", "symbol": symbol, "source": source,
            "timeframe": TIMEFRAME, "session": get_session(candle_time) if candle_time is not None else "",
            "raw_direction": raw_direction, "candle_time": str(candle_time) if candle_time is not None else "",
            "notional_usd": round(notional, 4), "leverage": leverage, "margin_usd": round(margin_needed, 4),
            "balance_after": round(state["balance"], 4),
        })
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
        "session": get_session(candle_time) if candle_time is not None else "",
        "raw_direction": raw_direction,
        "direction": direction,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "qty_total": qty_total,
        "notional": notional,
        "leverage": leverage,
        "lot_a": {"qty": qty_half, "target": "tp1", "status": "open"},
        "lot_b": {"qty": qty_total - qty_half, "target": "tp2", "status": "open"},
    }
    save_state(state)

    exposure = open_notional_sum(state)
    margin_used = open_margin_sum(state)
    emoji = "🟢" if direction == "long" else "🔴"
    candle_line = f"زمان کندل: {candle_time}\n" if candle_time is not None else ""
    send_telegram_message(
        f"{emoji} <b>#{trade_id} | پوزیشن فرضی {direction_label} باز شد (Paper Trading)</b> | {source}\n"
        f"نماد: <b>{symbol}</b>\n{candle_line}حجم: {qty_total:.6f}\nارزش معامله: {notional:.2f}$ (لوریج {leverage:g}x)\n"
        f"مارجین این معامله: {margin_needed:.2f}$\n"
        f"ورود: {entry_price:.6f}\nSL: {sl_price:.6f}\nTP1: {tp1_price:.6f}\nTP2: {tp2_price:.6f}\n"
        f"—\nموجودی نقدی: {state['balance']:.2f}$\n"
        f"مارجین درگیر در پوزیشن‌های باز: {margin_used:.2f}$\n"
        f"ارزش کل پوزیشن‌های باز (اسمی): {exposure:.2f}$"
    )

    log_trade_event({
        "event_time_utc": datetime.now(timezone.utc).isoformat(),
        "event_type": "open",
        "trade_id": trade_id,
        "symbol": symbol,
        "source": source,
        "timeframe": TIMEFRAME,
        "session": get_session(candle_time) if candle_time is not None else "",
        "raw_direction": raw_direction,
        "final_direction": direction,
        "candle_time": str(candle_time) if candle_time is not None else "",
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "notional_usd": round(notional, 4),
        "leverage": leverage,
        "margin_usd": round(margin_needed, 4),
        "adx_value": round(adx_value, 3) if adx_value is not None else "",
        "balance_after": round(state["balance"], 4),
    })


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
            log_trade_event({
                "event_time_utc": datetime.now(timezone.utc).isoformat(),
                "event_type": "lot_close", "trade_id": pos.get("trade_id", ""), "symbol": symbol,
                "source": pos["source"], "timeframe": TIMEFRAME, "session": pos.get("session", ""),
                "raw_direction": pos.get("raw_direction", ""), "final_direction": direction,
                "candle_time": pos.get("candle_time", ""),
                "lot": lot_key, "exit_reason": "sl", "exit_price": exit_price,
                "pnl": round(pnl, 4), "balance_after": round(state["balance"], 4),
                "leverage": pos.get("leverage", 1.0),
                "margin_usd": round((lot["qty"] * pos["entry_price"]) / (pos.get("leverage", 1.0) or 1.0), 4),
            })
        elif hit_tp:
            exit_price = target_price
            pnl = lot["qty"] * (exit_price - pos["entry_price"]) * (1 if is_long else -1)
            state["balance"] += pnl
            lot["status"] = "closed_tp"
            changed = True
            send_telegram_message(f"🟢 <b>#{pos.get('trade_id','?')} | {lot_key}</b> برای <b>{symbol}</b> ({pos['source']}) با حد سود بسته شد. (سود/ضرر: {pnl:+.2f}$)")
            log_trade_event({
                "event_time_utc": datetime.now(timezone.utc).isoformat(),
                "event_type": "lot_close", "trade_id": pos.get("trade_id", ""), "symbol": symbol,
                "source": pos["source"], "timeframe": TIMEFRAME, "session": pos.get("session", ""),
                "raw_direction": pos.get("raw_direction", ""), "final_direction": direction,
                "candle_time": pos.get("candle_time", ""),
                "lot": lot_key, "exit_reason": lot["target"], "exit_price": exit_price,
                "pnl": round(pnl, 4), "balance_after": round(state["balance"], 4),
                "leverage": pos.get("leverage", 1.0),
                "margin_usd": round((lot["qty"] * pos["entry_price"]) / (pos.get("leverage", 1.0) or 1.0), 4),
            })

            if lot_key == "lot_a" and pos["lot_b"]["status"] == "open":
                pos["sl_price"] = pos["entry_price"]
                send_telegram_message(
                    f"🛡 <b>#{pos.get('trade_id','?')} | حد ضرر پوزیشن {symbol} ({pos['source']}) به نقطه ورود ({pos['entry_price']:.6f}) منتقل شد (Risk-Free).</b>"
                )
                log_trade_event({
                    "event_time_utc": datetime.now(timezone.utc).isoformat(),
                    "event_type": "sl_to_be", "trade_id": pos.get("trade_id", ""), "symbol": symbol,
                    "source": pos["source"], "timeframe": TIMEFRAME, "session": pos.get("session", ""),
                    "raw_direction": pos.get("raw_direction", ""), "final_direction": direction,
                    "candle_time": pos.get("candle_time", ""), "sl_price": pos["sl_price"],
                })

    if changed:
        if pos["lot_a"]["status"] != "open" and pos["lot_b"]["status"] != "open":
            del state["positions"][key]
            exposure = open_margin_sum(state)
            send_telegram_message(
                f"✅ <b>#{pos.get('trade_id','?')}</b> | پوزیشن <b>{symbol}</b> ({pos['source']}) کاملاً بسته شد.\n"
                f"موجودی نقدی: {state['balance']:.2f}$  |  مارجین درگیر باقی‌مانده: {exposure:.2f}$"
            )
            log_trade_event({
                "event_time_utc": datetime.now(timezone.utc).isoformat(),
                "event_type": "full_close", "trade_id": pos.get("trade_id", ""), "symbol": symbol,
                "source": pos["source"], "timeframe": TIMEFRAME, "session": pos.get("session", ""),
                "raw_direction": pos.get("raw_direction", ""), "final_direction": direction,
                "candle_time": pos.get("candle_time", ""), "balance_after": round(state["balance"], 4),
            })
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

        # --- استراتژی ۱: Supertrend + ADX (تنها استراتژی فعال فعلاً) ---
        buy1, sell1, ct1, price1, st_line, adx_val1 = check_strategy_supertrend(df)
        if buy1:
            direction, sl, tp1, tp2 = resolve_direction_and_levels("long", price1, st_line, ST_TP1_RR, ST_TP2_RR)
            print(f"[{symbol}] سیگنال خرید Supertrend+ADX فعال شد -> تلاش برای باز کردن پوزیشن {direction} (معکوس)...")
            open_position(state, symbol, direction, price1, sl, tp1, tp2, source="Supertrend+ADX",
                          candle_time=ct1, raw_direction="long", adx_value=adx_val1)
        elif sell1:
            direction, sl, tp1, tp2 = resolve_direction_and_levels("short", price1, st_line, ST_TP1_RR, ST_TP2_RR)
            print(f"[{symbol}] سیگنال فروش Supertrend+ADX فعال شد -> تلاش برای باز کردن پوزیشن {direction} (معکوس)...")
            open_position(state, symbol, direction, price1, sl, tp1, tp2, source="Supertrend+ADX",
                          candle_time=ct1, raw_direction="short", adx_value=adx_val1)

        # --- استراتژی ۲: ICT/SMC Scalp Pro (فعلاً خاموش - نیاز به داده‌ی بیشتر) ---
        if ENABLE_SMC:
            try:
                htf_bullish, htf_bearish = get_htf_bias(symbol)
            except Exception:
                htf_bullish, htf_bearish = True, True

            res = check_strategy_smc(df, htf_bullish, htf_bearish)
            if res is not None and res["buy"]:
                price2 = res["price"]
                atr2 = res["atr"]
                raw_sl = price2 - atr2 * SL_ATR_MULT
                direction, sl, tp1, tp2 = resolve_direction_and_levels("long", price2, raw_sl, TP1_RR, TP2_RR)
                print(f"[{symbol}] سیگنال خرید ICT/SMC فعال شد (امتیاز={res['bull_score']}/7) -> تلاش برای باز کردن پوزیشن {direction} (معکوس)...")
                open_position(state, symbol, direction, price2, sl, tp1, tp2, source="ICT/SMC Scalp Pro",
                              candle_time=res["candle_time"], raw_direction="long")
            elif res is not None and res["sell"]:
                price2 = res["price"]
                atr2 = res["atr"]
                raw_sl = price2 + atr2 * SL_ATR_MULT
                direction, sl, tp1, tp2 = resolve_direction_and_levels("short", price2, raw_sl, TP1_RR, TP2_RR)
                print(f"[{symbol}] سیگنال فروش ICT/SMC فعال شد (امتیاز={res['bear_score']}/7) -> تلاش برای باز کردن پوزیشن {direction} (معکوس)...")
                open_position(state, symbol, direction, price2, sl, tp1, tp2, source="ICT/SMC Scalp Pro",
                              candle_time=res["candle_time"], raw_direction="short")
            elif res is not None:
                print(f"[{symbol}] بدون سیگنال SMC جدید (امتیاز خرید={res['bull_score']}/7, امتیاز فروش={res['bear_score']}/7)")

        time.sleep(0.3)

    save_state(state)


if __name__ == "__main__":
    main()
