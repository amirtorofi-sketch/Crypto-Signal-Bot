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
from signal_bot_v2 import check_strategy_poc_retest

# =====================================================================
# تنظیمات
# =====================================================================
STARTING_BALANCE = 1000.0      # موجودی فرضی اولیه (دلار مجازی)

# فعلاً فقط Supertrend+ADX فعاله (به‌خاطر نیاز به داده‌ی بیشتر برای ICT/SMC خاموش شد)
# هر دو استراتژی فعالن: Supertrend+ADX (با لوریج 3x) و ICT/SMC Scalp Pro (بدون لوریج، عادی)
ENABLE_SMC = True
ENABLE_POC = True   # POC Retest معکوس با لوریج ۱۰ و مارجین ۱۰$ (همون استراتژی dasttrade2 ولی جهت برعکس)

# حجم هر معامله به تفکیک استراتژی (چون Win Rate بالای Supertrend+ADX توجیه‌کننده‌ی حجم بیشتره)
TRADE_AMOUNT_BY_SOURCE = {
    "Supertrend+ADX": 300.0,
    "ICT/SMC Scalp Pro": 100.0,
    "POC Retest (Reversed)": 100.0,
}
DEFAULT_TRADE_AMOUNT = 100.0

# لوریج به تفکیک استراتژی: مارجین واقعی کم‌شده از موجودی = حجم معامله / لوریج
# (سود/ضرر همچنان بر مبنای کل حجم معامله محاسبه می‌شود، چون خودِ لوریج یعنی همین)
LEVERAGE_BY_SOURCE = {
    "Supertrend+ADX": 3.0,
    "ICT/SMC Scalp Pro": 1.0,
    "POC Retest (Reversed)": 10.0,   # مارجین هر معامله = 100 / 10 = 10$
}
DEFAULT_LEVERAGE = 1.0

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")
TRADES_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_log.csv")

SOURCE_TAG = {"Supertrend+ADX": "ST", "ICT/SMC Scalp Pro": "SMC", "POC Retest (Reversed)": "POC"}

TRADES_LOG_FIELDS = [
    "event_time_utc", "event_type", "trade_id", "symbol", "source", "timeframe", "session",
    "raw_direction", "final_direction", "candle_time",
    "entry_price", "sl_price", "tp1_price", "tp2_price", "notional_usd", "leverage", "margin_usd",
    "adx_value", "signal_score", "market_snapshot",
    "lot", "exit_reason", "exit_price", "pnl", "balance_after",
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


# ---------------------------------------------------------------------
# فیلترهای ICT/SMC (بر پایه‌ی تحلیل دیتا): نماد حذف‌شده، سشن‌های حذف‌شده، و لوریج سشن آسیا
# سشن‌ها همان تعریف تحلیل (ساعت UTC کندل سیگنال): Asia <7 | London <12 | London-NY Overlap <16 | New York <21 | Off-hours
# ---------------------------------------------------------------------
SMC_EXCLUDED_SYMBOLS = {"ONEUSDT"}
SMC_EXCLUDED_SESSIONS = {"New York", "London-NY Overlap"}
SMC_ASIA_LEVERAGE = 3.0   # مارجین هر معامله مثل قبل (۱۰۰$) می‌ماند؛ ارزش معامله = ۱۰۰ × ۳ = ۳۰۰$


def analysis_session(dt) -> str:
    try:
        h = dt.hour if hasattr(dt, "hour") else datetime.fromisoformat(str(dt)).hour
    except Exception:
        return ""
    if h < 7:
        return "Asia"
    if h < 12:
        return "London"
    if h < 16:
        return "London-NY Overlap"
    if h < 21:
        return "New York"
    return "Off-hours"


def _migrate_log_if_needed():
    """
    اگه هدر فایل با TRADES_LOG_FIELDS فعلی فرق داشته باشه (مثلاً وقتی ستون جدیدی
    اضافه شده)، کل فایل یک‌بار بازنویسی می‌شه: هر ردیف قدیمی (با تعداد ستون هدر قدیمی)
    بر اساس همون هدر قدیمی به ستون‌های درست جدید نگاشته می‌شه و ستون‌های تازه برای
    اون‌ها خالی می‌مونه؛ ردیف‌های جدید (که از قبل با ترتیب جدید نوشته شدن) دست‌نخورده
    می‌مونن. بدون این کار، ردیف‌های جدید زیر هدر قدیمی جابه‌جا و نامرتب نشون داده می‌شن.
    """
    if not os.path.exists(TRADES_LOG_FILE):
        return
    try:
        with open(TRADES_LOG_FILE, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        if not rows or rows[0] == TRADES_LOG_FIELDS:
            return
        old_header = rows[0]
        migrated = []
        for r in rows[1:]:
            if not r:
                continue
            if len(r) == len(TRADES_LOG_FIELDS):
                d = dict(zip(TRADES_LOG_FIELDS, r))       # ردیف جدید
            elif len(r) == len(old_header):
                d = dict(zip(old_header, r))              # ردیف قدیمی
            else:
                d = dict(zip(old_header, r))              # حالت غیرمنتظره: بهترین تلاش
            migrated.append([d.get(field, "") for field in TRADES_LOG_FIELDS])
        tmp = TRADES_LOG_FILE + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(TRADES_LOG_FIELDS)
            w.writerows(migrated)
        os.replace(tmp, TRADES_LOG_FILE)
        print(f"trades_log.csv به هدر جدید مهاجرت داده شد ({len(migrated)} ردیف).")
    except Exception as e:
        print(f"خطا در مهاجرت هدر trades_log.csv: {e}")


def log_trade_event(row: dict):
    """یک ردیف جدید به trades_log.csv اضافه می‌کند (append-only، تاریخچه‌ی کامل و دائمی)."""
    _migrate_log_if_needed()
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


def _entry_details(pos: dict) -> dict:
    """
    جزئیات ورودِ پوزیشن (همون چیزی که روی ردیف open ثبت شده) برای تکرار روی ردیف‌های
    lot_close / sl_to_be / full_close، تا هر ردیف به‌تنهایی قابل تحلیل باشه.
    sl_price همیشه SL اولیه است (بعد از TP1، SL پوزیشن به نقطه ورود می‌رود)؛ برای
    پوزیشن‌های قدیمی که sl_initial ندارن همون sl_price فعلی استفاده می‌شه.
    notional_usd / margin_usd مقدار کل پوزیشن‌اند (نه فقط یک لات)؛ سهم لات از ستون lot
    و pnl همون ردیف مشخصه.
    """
    lev = pos.get("leverage", 1.0) or 1.0
    notional = pos.get("notional")
    snap = pos.get("market_snapshot")
    score = pos.get("signal_score")
    adx = pos.get("adx_value")
    return {
        "entry_price": pos.get("entry_price", ""),
        "sl_price": pos.get("sl_initial", pos.get("sl_price", "")),
        "tp1_price": pos.get("tp1_price", ""),
        "tp2_price": pos.get("tp2_price", ""),
        "notional_usd": round(notional, 4) if notional is not None else "",
        "leverage": pos.get("leverage", ""),
        "margin_usd": round(notional / lev, 4) if notional is not None else "",
        "adx_value": round(adx, 3) if adx is not None else "",
        "signal_score": score if score is not None else "",
        "market_snapshot": json.dumps(snap, ensure_ascii=False) if snap is not None else "",
    }


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


def resolve_poc_reversed(raw_direction: str, entry: float, raw_sl: float, raw_tp: float):
    """
    POC Retest معکوس: جهت سیگنال برعکس می‌شه و فاصله‌ی ریسک (تا SL) و فاصله‌ی سود (تا TP)
    عیناً به‌صورت آینه‌ای حول قیمت ورود در سمت مخالف بازتعریف می‌شن (RR ثابت می‌مونه).
    تک‌هدفیه: فقط یک SL و یک TP.
    """
    risk = abs(entry - raw_sl)
    reward = abs(raw_tp - entry)
    if raw_direction == "long":
        return "short", entry + risk, entry - reward
    return "long", entry - risk, entry + reward


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
def calculate_position_size(entry_price: float, source: str, amount_override=None):
    if entry_price <= 0:
        return 0.0
    amount = amount_override if amount_override else TRADE_AMOUNT_BY_SOURCE.get(source, DEFAULT_TRADE_AMOUNT)
    return amount / entry_price


# =====================================================================
# باز کردن پوزیشن فرضی جدید (Long یا Short)
# =====================================================================
def open_position(state: dict, symbol: str, direction: str, entry_price: float, sl_price: float,
                  tp1_price: float, tp2_price: float, source: str, candle_time=None,
                  raw_direction: str = "", adx_value=None, signal_score=None, market_snapshot=None,
                  amount_override=None, leverage_override=None):
    key = position_key(symbol, source)

    if key in state["positions"]:
        existing_id = state["positions"][key].get("trade_id", "?")
        send_telegram_message(
            f"⏭ سیگنال جدید {source} برای <b>{symbol}</b> دریافت شد، ولی چون پوزیشن باز #{existing_id} "
            f"از همین استراتژی روی این نماد داری، نادیده گرفته شد (تا پوزیشن قبلی بسته شود)."
        )
        return

    qty_total = calculate_position_size(entry_price, source, amount_override)
    notional = qty_total * entry_price

    if qty_total <= 0 or notional < 5:
        print(f"[{symbol}] حجم/ارزش معامله نامعتبر (qty={qty_total:.6f}, notional={notional:.2f}), رد شد.")
        return

    leverage = leverage_override or LEVERAGE_BY_SOURCE.get(source, DEFAULT_LEVERAGE) or DEFAULT_LEVERAGE
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

    single_target = (source == "POC Retest (Reversed)")   # تک‌هدفی: کل حجم روی lot_a، بدون TP2 و بدون Risk-Free
    qty_half = qty_total if single_target else qty_total / 2
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
        "sl_initial": sl_price,          # SL اولیه (بعد از TP1، sl_price به نقطه ورود می‌رود)
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "adx_value": adx_value,
        "qty_total": qty_total,
        "notional": notional,
        "leverage": leverage,
        "signal_score": signal_score,
        "market_snapshot": market_snapshot,
        "lot_a": {"qty": qty_half, "target": "tp1", "status": "open"},
        "lot_b": ({"qty": 0.0, "target": "tp2", "status": "none"} if single_target
                  else {"qty": qty_total - qty_half, "target": "tp2", "status": "open"}),
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
        f"ورود: {entry_price:.6f}\nSL: {sl_price:.6f}\n"
        + (f"TP: {tp1_price:.6f}\n" if tp2_price is None else f"TP1: {tp1_price:.6f}\nTP2: {tp2_price:.6f}\n") +
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
        "signal_score": signal_score if signal_score is not None else "",
        "market_snapshot": json.dumps(market_snapshot, ensure_ascii=False) if market_snapshot is not None else "",
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
            log_trade_event({**_entry_details(pos),
                "event_time_utc": datetime.now(timezone.utc).isoformat(),
                "event_type": "lot_close", "trade_id": pos.get("trade_id", ""), "symbol": symbol,
                "source": pos["source"], "timeframe": TIMEFRAME, "session": pos.get("session", ""),
                "raw_direction": pos.get("raw_direction", ""), "final_direction": direction,
                "candle_time": pos.get("candle_time", ""),
                "lot": lot_key, "exit_reason": "sl", "exit_price": exit_price,
                "pnl": round(pnl, 4), "balance_after": round(state["balance"], 4),
            })
        elif hit_tp:
            exit_price = target_price
            pnl = lot["qty"] * (exit_price - pos["entry_price"]) * (1 if is_long else -1)
            state["balance"] += pnl
            lot["status"] = "closed_tp"
            changed = True
            send_telegram_message(f"🟢 <b>#{pos.get('trade_id','?')} | {lot_key}</b> برای <b>{symbol}</b> ({pos['source']}) با حد سود بسته شد. (سود/ضرر: {pnl:+.2f}$)")
            log_trade_event({**_entry_details(pos),
                "event_time_utc": datetime.now(timezone.utc).isoformat(),
                "event_type": "lot_close", "trade_id": pos.get("trade_id", ""), "symbol": symbol,
                "source": pos["source"], "timeframe": TIMEFRAME, "session": pos.get("session", ""),
                "raw_direction": pos.get("raw_direction", ""), "final_direction": direction,
                "candle_time": pos.get("candle_time", ""),
                "lot": lot_key, "exit_reason": lot["target"], "exit_price": exit_price,
                "pnl": round(pnl, 4), "balance_after": round(state["balance"], 4),
            })

            if lot_key == "lot_a" and pos["lot_b"]["status"] == "open":
                pos["sl_price"] = pos["entry_price"]
                send_telegram_message(
                    f"🛡 <b>#{pos.get('trade_id','?')} | حد ضرر پوزیشن {symbol} ({pos['source']}) به نقطه ورود ({pos['entry_price']:.6f}) منتقل شد (Risk-Free).</b>"
                )
                log_trade_event({**_entry_details(pos),
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
            log_trade_event({**_entry_details(pos),
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
        buy1, sell1, ct1, price1, st_line, adx_val1, snap1 = check_strategy_supertrend(df)
        if buy1:
            direction, sl, tp1, tp2 = resolve_direction_and_levels("long", price1, st_line, ST_TP1_RR, ST_TP2_RR)
            print(f"[{symbol}] سیگنال خرید Supertrend+ADX فعال شد -> تلاش برای باز کردن پوزیشن {direction} (معکوس)...")
            open_position(state, symbol, direction, price1, sl, tp1, tp2, source="Supertrend+ADX",
                          candle_time=ct1, raw_direction="long", adx_value=adx_val1, market_snapshot=snap1)
        elif sell1:
            direction, sl, tp1, tp2 = resolve_direction_and_levels("short", price1, st_line, ST_TP1_RR, ST_TP2_RR)
            print(f"[{symbol}] سیگنال فروش Supertrend+ADX فعال شد -> تلاش برای باز کردن پوزیشن {direction} (معکوس)...")
            open_position(state, symbol, direction, price1, sl, tp1, tp2, source="Supertrend+ADX",
                          candle_time=ct1, raw_direction="short", adx_value=adx_val1, market_snapshot=snap1)

        # --- استراتژی ۲: ICT/SMC Scalp Pro (فعلاً خاموش - نیاز به داده‌ی بیشتر) ---
        if ENABLE_SMC and symbol not in SMC_EXCLUDED_SYMBOLS:
            try:
                htf_bullish, htf_bearish = get_htf_bias(symbol)
            except Exception:
                htf_bullish, htf_bearish = True, True

            res = check_strategy_smc(df, htf_bullish, htf_bearish)
            smc_ov = {}
            if res is not None and (res["buy"] or res["sell"]):
                smc_session = analysis_session(res["candle_time"])
                if smc_session in SMC_EXCLUDED_SESSIONS:
                    print(f"[{symbol}] سیگنال ICT/SMC در سشن {smc_session} نادیده گرفته شد (سشن حذف‌شده).")
                    res = None
                elif smc_session == "Asia":
                    smc_ov = {"leverage_override": SMC_ASIA_LEVERAGE,
                              "amount_override": TRADE_AMOUNT_BY_SOURCE["ICT/SMC Scalp Pro"] * SMC_ASIA_LEVERAGE}
            if res is not None and res["buy"]:
                price2 = res["price"]
                atr2 = res["atr"]
                raw_sl = price2 - atr2 * SL_ATR_MULT
                direction, sl, tp1, tp2 = resolve_direction_and_levels("long", price2, raw_sl, TP1_RR, TP2_RR)
                print(f"[{symbol}] سیگنال خرید ICT/SMC فعال شد (امتیاز={res['bull_score']}/7) -> تلاش برای باز کردن پوزیشن {direction} (معکوس)...")
                open_position(state, symbol, direction, price2, sl, tp1, tp2, source="ICT/SMC Scalp Pro",
                              candle_time=res["candle_time"], raw_direction="long",
                              signal_score=res["bull_score"], market_snapshot=res.get("confluence"), **smc_ov)
            elif res is not None and res["sell"]:
                price2 = res["price"]
                atr2 = res["atr"]
                raw_sl = price2 + atr2 * SL_ATR_MULT
                direction, sl, tp1, tp2 = resolve_direction_and_levels("short", price2, raw_sl, TP1_RR, TP2_RR)
                print(f"[{symbol}] سیگنال فروش ICT/SMC فعال شد (امتیاز={res['bear_score']}/7) -> تلاش برای باز کردن پوزیشن {direction} (معکوس)...")
                open_position(state, symbol, direction, price2, sl, tp1, tp2, source="ICT/SMC Scalp Pro",
                              candle_time=res["candle_time"], raw_direction="short",
                              signal_score=res["bear_score"], market_snapshot=res.get("confluence"), **smc_ov)
            elif res is not None:
                print(f"[{symbol}] بدون سیگنال SMC جدید (امتیاز خرید={res['bull_score']}/7, امتیاز فروش={res['bear_score']}/7)")

        # --- استراتژی ۳: POC Retest (معکوس، بدون لوریج، تک‌هدفی) ---
        if ENABLE_POC and f"{symbol}__POC" not in state["positions"]:
            sig = check_strategy_poc_retest(df)
            if sig is not None:
                direction, sl, tp = resolve_poc_reversed(sig.direction, sig.entry, sig.stop_loss, sig.take_profit)
                snap = {
                    "poc": sig.poc, "leg_start": str(sig.leg_start), "leg_end": str(sig.leg_end),
                    "rr": round(sig.rr, 3) if sig.rr == sig.rr else None, "reason": sig.reason,
                    "raw_sl": sig.stop_loss, "raw_tp": sig.take_profit,
                }
                print(f"[{symbol}] سیگنال POC Retest ({sig.direction}) -> باز کردن پوزیشن {direction} (معکوس)...")
                open_position(state, symbol, direction, sig.entry, sl, tp, None, source="POC Retest (Reversed)",
                              candle_time=sig.time, raw_direction=sig.direction, market_snapshot=snap)

        time.sleep(0.3)

    save_state(state)


if __name__ == "__main__":
    main()
