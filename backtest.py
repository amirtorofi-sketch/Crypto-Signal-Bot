"""
بک‌تستر ربات دست‌ترید۱ (Supertrend+ADX معکوس + ICT/SMC Scalp Pro معکوس + ICT/SMC v2 معکوس + POC Retest معکوس؛ هر چهار استراتژی ربات زنده).

اصل طراحی: *هیچ منطقی بازنویسی نشده*. همان `trading_bot.main()` واقعی هر ۱۵ دقیقه (برای هر کندل
تاریخی) اجرا می‌شود و فقط ورودی/خروجی‌هایش جایگزین می‌شود:
  - get_klines        -> ۳۰۰ کندل منتهی به کندلِ «در حال تشکیل» (دقیقاً مثل API زنده)
  - get_htf_bias      -> از روی کندل‌های ۱ ساعته‌ی ساخته‌شده از ۱۵ دقیقه‌ای (۱۰۰ کندل، EMA50، کندلِ ماقبل‌آخر)
  - تلگرام/دیسک/sleep -> بی‌اثر؛ لاگ معاملات در حافظه جمع می‌شود
  - FORCE_CLOSE_IF_CANDLE_BEFORE -> خاموش (وگرنه کل تاریخچه قدیمی «بستن دستی» می‌شد)
بنابراین فیلتر سشن/نماد/SL تنگ، معکوس‌سازی، دو لات، Risk-Free بعد از TP1، ترتیب «SL قبل از TP»
داخل کندل و سقف مارجین همه عیناً همان کد زنده‌اند.

اجرا (از ریشه‌ی ریپو، بعد از download_klines.py):
    python backtest.py --data data --fee-bps 10 --workers 4
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

WINDOW = 300          # KLINES_LIMIT زنده
HTF_N = 100           # get_klines(symbol, "1h", 100) زنده


def load_symbol(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["open_time"])
    return df.sort_values("open_time").reset_index(drop=True)


def make_1h(df15: pd.DataFrame) -> pd.DataFrame:
    g = df15.set_index("open_time").resample("1h")
    h = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                      "close": g["close"].last(), "volume": g["volume"].sum()}).dropna(subset=["close"])
    return h.reset_index()


def supertrend_fast(df, atr_len, factor):
    """
    همان signal_bot.supertrend (خط‌به‌خط همان شرط‌ها) ولی روی لیست‌های پایتون به‌جای .iloc
    (۱۰۰ برابر سریع‌تر). برابری دقیق با نسخه‌ی اصلی در selftest_supertrend() تست می‌شود.
    """
    import signal_bot as sb
    hl2 = ((df["high"] + df["low"]) / 2).tolist()
    atr_val = sb.atr(df, atr_len).tolist()
    close = df["close"].tolist()
    n = len(close)
    up = [hl2[i] + factor * atr_val[i] for i in range(n)]
    lo = [hl2[i] - factor * atr_val[i] for i in range(n)]
    fu, fl, st, d = up[:], lo[:], [0.0] * n, [0] * n
    st[0], d[0] = fu[0], 1
    for i in range(1, n):
        fu[i] = up[i] if (up[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lo[i] if (lo[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        if st[i - 1] == fu[i - 1]:
            if close[i] <= fu[i]:
                st[i], d[i] = fu[i], 1
            else:
                st[i], d[i] = fl[i], -1
        else:
            if close[i] >= fl[i]:
                st[i], d[i] = fl[i], -1
            else:
                st[i], d[i] = fu[i], 1
    return pd.Series(st, index=df.index, dtype=float), pd.Series(d, index=df.index, dtype=int)


def selftest_supertrend(df15: pd.DataFrame, samples: int = 300) -> None:
    """supertrend_fast را روی پنجره‌های واقعی با نسخه‌ی اصلی مقایسه می‌کند؛ هر اختلافی = توقف."""
    import signal_bot as sb
    idx = np.linspace(WINDOW, len(df15), samples, dtype=int)
    for e in idx:
        w = df15.iloc[e - WINDOW:e].reset_index(drop=True)
        a_st, a_d = sb.supertrend(w, sb.ATR_PERIOD, sb.ST_FACTOR)
        b_st, b_d = supertrend_fast(w, sb.ATR_PERIOD, sb.ST_FACTOR)
        if not (np.array_equal(a_st.values, b_st.values) and np.array_equal(a_d.values, b_d.values)):
            raise SystemExit(f"❌ supertrend_fast با نسخه‌ی اصلی فرق دارد (پنجره‌ی منتهی به {e})")


def run_symbol(args):
    symbol, path, only, start_ts = args
    sys.path.insert(0, os.getcwd())
    sys.stdout = open(os.devnull, "w")   # main() واقعی خیلی print می‌کند؛ ساکتش می‌کنیم
    from datetime import datetime
    import signal_bot as sb
    import trading_bot as tb

    df = load_symbol(path)
    n = len(df)
    sb.supertrend = supertrend_fast      # فقط سرعت؛ برابری با اصلی در selftest تست می‌شود
    if n < WINDOW + 5:
        return symbol, [], {"error": f"داده‌ی کافی نیست ({n} کندل)"}
    h1 = make_1h(df)
    h1_times = h1["open_time"].values
    h1_close = h1["close"]

    clock = [None]

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    state = {"balance": tb.STARTING_BALANCE, "positions": {}, "next_trade_id": 1,
             "balance_reset_marker": tb.BALANCE_RESET_MARKER}
    rows = []
    cur = {"i": 0}

    def fake_klines(sym, tf, limit=WINDOW):
        i = cur["i"]
        return df.iloc[i - WINDOW + 1: i + 1].reset_index(drop=True)

    def fake_htf(sym):
        i = cur["i"]
        hour = df["open_time"].iloc[i].floor("1h")
        j = int(np.searchsorted(h1_times, np.datetime64(hour), side="right"))   # شامل کندل ۱h در حال تشکیل
        close = h1_close.iloc[max(0, j - HTF_N): j].reset_index(drop=True)
        ema_htf = sb.ema(close, sb.HTF_EMA_LEN)
        return bool(close.iloc[-2] > ema_htf.iloc[-2]), bool(close.iloc[-2] < ema_htf.iloc[-2])

    class _NoSleep:
        @staticmethod
        def sleep(_):
            return None

        def __getattr__(self, k):
            import time as _t
            return getattr(_t, k)

    tb.SYMBOLS = [symbol]
    tb.get_klines = fake_klines
    tb.get_htf_bias = fake_htf
    tb.get_htf_bias_v2 = fake_htf      # منطق v2 دقیقاً همان است (۱۰۰ کندل ۱h، EMA50، کندل ماقبل‌آخر)
    tb.send_telegram_message = lambda *a, **k: None
    tb.save_state = lambda s: None
    tb.load_state = lambda: state
    tb.time = _NoSleep()
    tb.datetime = _DT
    tb.FORCE_CLOSE_IF_CANDLE_BEFORE = "0000-00-00"
    tb.ENABLE_SMC = "smc" in only
    tb.ENABLE_POC = "poc" in only
    tb.ENABLE_SMC_V2 = "smc2" in only
    if "st" not in only:
        tb.check_strategy_supertrend = lambda d: (False, False, None, None, None, None, None)
    tb.log_trade_event = lambda row: rows.append({**row, "event_time_utc": clock[0].isoformat()})

    first = WINDOW - 1
    if start_ts is not None:
        first = max(first, int(np.searchsorted(df["open_time"].values, np.datetime64(start_ts))))
    for i in range(first, n):
        cur["i"] = i
        clock[0] = (df["open_time"].iloc[i] + pd.Timedelta(minutes=1)).to_pydatetime().replace(
            tzinfo=__import__("datetime").timezone.utc)
        tb.main()

    meta = {"candles": n, "first": str(df["open_time"].iloc[first]), "last": str(df["open_time"].iloc[-1]),
            "still_open": len(state["positions"])}
    return symbol, rows, meta


def pf(x):
    w = x[x > 0].sum()
    l = -x[x < 0].sum()
    return float(w / l) if l > 0 else float("inf")


def summarize(log: pd.DataFrame, fee_bps: float):
    from trading_bot import analysis_session
    lots = log[log.event_type == "lot_close"].copy()
    opens = log[log.event_type == "open"].copy()
    if lots.empty:
        print("هیچ معامله‌ی بسته‌شده‌ای نبود.")
        return
    lots["pnl"] = lots["pnl"].astype(float)
    lots["notional_usd"] = lots["notional_usd"].astype(float)
    fee = fee_bps / 10000.0
    # هزینه‌ی رفت‌وبرگشت هر لات: نصف ارزش پوزیشن؛ ولی POC تک‌هدفی است (کل حجم روی یک لات)
    lots["fee"] = np.where(lots["source"].str.startswith("POC"), lots["notional_usd"], lots["notional_usd"] / 2) * fee
    lots["net"] = lots["pnl"] - lots["fee"]
    lots["t"] = pd.to_datetime(lots["event_time_utc"])
    lots["session"] = lots["candle_time"].map(lambda s: analysis_session(pd.Timestamp(s)))
    mid = lots["t"].quantile(0.5)
    pd.set_option("display.width", 200)
    print(f"\nبازه: {lots.t.min():%Y-%m-%d} تا {lots.t.max():%Y-%m-%d} | کارمزد رفت‌وبرگشت: {fee_bps} bps از ارزش هر لات")

    def tab(g):
        pos = g.groupby(["symbol", "trade_id"]).size().shape[0]
        return pd.Series({"لات": len(g), "پوزیشن": pos, "WR%": (g.pnl > 0).mean() * 100,
                          "PF_خام": pf(g.pnl), "PF_بعد_کارمزد": pf(g.net),
                          "Exp_خام$": g.pnl.mean(), "Exp_خالص$": g.net.mean(),
                          "Net_خام$": g.pnl.sum(), "Net_خالص$": g.net.sum()})
    print("\n== هر استراتژی ==")
    print(lots.groupby("source").apply(tab).round(3).to_string())
    print("\n== استراتژی × سشن (UTC کندل سیگنال) ==")
    print(lots.groupby(["source", "session"]).apply(tab).round(3).to_string())
    print("\n== پایداری: نیمه‌ی اول / دوم بازه ==")
    lots["half"] = np.where(lots.t < mid, "نیمه ۱", "نیمه ۲")
    print(lots.groupby(["source", "half"]).apply(tab)[["لات", "PF_خام", "PF_بعد_کارمزد", "Net_خالص$"]].round(3).to_string())
    print("\n== سود خالص ماهانه ==")
    lots["month"] = lots.t.dt.strftime("%Y-%m")
    print(lots.pivot_table(index="month", columns="source", values="net", aggfunc="sum").round(1).to_string())
    print("\nنکات: ۱) ترتیب داخل کندل: SL قبل از TP (مثل کد زنده). ۲) ورود دقیقاً به close کندل سیگنال، بدون اسلیپیج. "
          "۳) برچسب exit_reason='sl' شامل خروج Breakeven هم می‌شود (PnL≈0).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="trades_log_backtest.csv")
    ap.add_argument("--fee-bps", type=float, default=10.0, help="کارمزد رفت‌وبرگشت (bps) برای محاسبه‌ی Net خالص")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--start", default=None, help="شروع بک‌تست (YYYY-MM-DD)؛ ۳۰۰ کندل قبلش برای گرم‌شدن لازم است")
    ap.add_argument("--only", default="st,smc,smc2,poc",
                    help="استراتژی‌ها (جداشده با ویرگول): st=Supertrend، smc=ICT/SMC، smc2=ICT/SMC v2، poc=POC Retest")
    a = ap.parse_args()
    sys.path.insert(0, os.getcwd())
    from trading_bot import TRADES_LOG_FIELDS

    files = sorted(f for f in os.listdir(a.data) if f.endswith("_15m.csv"))
    syms = [f.replace("_15m.csv", "") for f in files]
    if a.symbols:
        syms = [s for s in syms if s in a.symbols]
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    bad = only - {"st", "smc", "smc2", "poc"}
    if bad:
        raise SystemExit(f"استراتژی نامعتبر: {bad}")
    start_ts = pd.Timestamp(a.start) if a.start else None
    sample = load_symbol(os.path.join(a.data, files[0]))
    selftest_supertrend(sample, 200)
    print("✅ selftest: supertrend_fast دقیقاً برابر نسخه‌ی اصلی است")
    jobs = [(s, os.path.join(a.data, f"{s}_15m.csv"), only, start_ts) for s in syms]
    all_rows, metas = [], {}
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(run_symbol, j): j[0] for j in jobs}
        for f in as_completed(futs):
            sym, rows, meta = f.result()
            metas[sym] = meta
            for r in rows:
                r["_sym"] = sym
            all_rows.extend(rows)
            print(f"[{sym}] تمام شد: {len(rows)} رویداد | {meta}")
    if not all_rows:
        print("هیچ رویدادی ثبت نشد.")
        return
    log = pd.DataFrame(all_rows)
    log["_t"] = pd.to_datetime(log["event_time_utc"])
    log = log.sort_values(["_t"], kind="stable").reset_index(drop=True)
    # شماره‌ی معامله‌ی یکتا (هر نماد در پردازش خودش از ۱ شروع کرده بود)
    opens = log[log.event_type == "open"][["_sym", "trade_id"]].drop_duplicates()
    idmap = {(sy, tid): k + 1 for k, (sy, tid) in enumerate(zip(opens['_sym'], opens['trade_id']))}
    log["trade_id"] = [idmap.get((s, t), t) for s, t in zip(log["_sym"], log["trade_id"])]
    # موجودی تجمعی کل (هر نماد جدا از ۱۰۰۰۰ شروع کرده بود؛ اینجا یک حساب واحد)
    start_bal = 10000.0
    pnl = pd.to_numeric(log["pnl"], errors="coerce").fillna(0.0)
    log["balance_after"] = (start_bal + pnl.cumsum()).round(4)
    out = log[TRADES_LOG_FIELDS]
    out.to_csv(a.out, index=False)
    print(f"\nخروجی: {a.out} ({len(out)} ردیف، همان ستون‌های trades_log.csv)")
    summarize(log, a.fee_bps)


if __name__ == "__main__":
    main()
