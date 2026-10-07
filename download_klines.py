"""
دانلود کندل‌های ۱۵ دقیقه‌ای اسپات بایننس از آرشیو عمومی data.binance.vision (رایگان، بدون API Key).

- ماه‌های کامل  -> فایل ZIP ماهانه
- ماه جاری      -> فایل‌های ZIP روزانه
- روزی که فایلش نبود (یا هنوز آپلود نشده) -> REST عمومی data-api.binance.vision (صفحه‌بندی ۱۰۰۰تایی)
- نکته‌ی مهم: از ۱ ژانویه ۲۰۲۵ تایم‌استمپ فایل‌های اسپات «میکروثانیه» است، قبلش میلی‌ثانیه؛ هر دو پشتیبانی می‌شود.
- خروجی: data/<SYMBOL>_15m.csv  (open_time[UTC], open, high, low, close, volume)
- در پایان برای هر نماد «گزارش حفره» چاپ می‌شود (کندل‌های ۱۵ دقیقه‌ای که در داده نیستند).

اجرا:  python download_klines.py --days 365
       python download_klines.py --days 365 --symbols BTCUSDT ETHUSDT
"""
import argparse
import io
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

ARCHIVE = "https://data.binance.vision/data/spot"
REST = "https://data-api.binance.vision/api/v3/klines"
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_vol", "trades", "taker_base", "taker_quote", "ignore"]
INTERVAL = "15m"
STEP = pd.Timedelta(minutes=15)


def _to_dt(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series)
    # میکروثانیه (۲۰۲۵ به بعد) ~1.7e15 ، میلی‌ثانیه ~1.7e12
    unit = "us" if s.iloc[0] > 1e14 else "ms"
    return pd.to_datetime(s, unit=unit)


def parse_zip_bytes(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            raw = pd.read_csv(f, header=None, names=COLS, dtype=str)
    # بعضی فایل‌ها هدر دارند
    if not raw.empty and not str(raw["open_time"].iloc[0]).strip().isdigit():
        raw = raw.iloc[1:]
    if raw.empty:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame({"open_time": _to_dt(raw["open_time"])})
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = raw[c].astype(float).values
    return df


def fetch_zip(url: str):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return parse_zip_bytes(r.content)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_rest(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    out, cur = [], int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    while cur < end_ms:
        for attempt in range(4):
            try:
                r = requests.get(REST, params={"symbol": symbol, "interval": INTERVAL,
                                               "startTime": cur, "endTime": end_ms, "limit": 1000}, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
        else:
            break
        if not data:
            break
        out.extend(data)
        cur = data[-1][0] + 1
        time.sleep(0.2)
    if not out:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(out, columns=COLS)
    res = pd.DataFrame({"open_time": pd.to_datetime(df["open_time"], unit="ms")})
    for c in ["open", "high", "low", "close", "volume"]:
        res[c] = df[c].astype(float).values
    return res


def month_starts(start: datetime, end: datetime):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def download_symbol(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    parts = []
    first_of_this_month = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for y, m in month_starts(start, end):
        mstart = datetime(y, m, 1, tzinfo=timezone.utc)
        if mstart >= first_of_this_month:
            # ماه جاری: روزبه‌روز
            d = mstart
            while d.date() < end.date():          # امروز ناقص است، از REST می‌آید
                url = f"{ARCHIVE}/daily/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{d:%Y-%m-%d}.zip"
                df = fetch_zip(url)
                if df is None:
                    df = fetch_rest(symbol, d, d + timedelta(days=1))
                parts.append(df)
                d += timedelta(days=1)
        else:
            url = f"{ARCHIVE}/monthly/klines/{symbol}/{INTERVAL}/{symbol}-{INTERVAL}-{y}-{m:02d}.zip"
            df = fetch_zip(url)
            if df is None:   # ماهی که فایلش نیست -> REST
                nxt = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc)
                df = fetch_rest(symbol, mstart, nxt)
            parts.append(df)
    # امروزِ ناقص (و هر شکافِ انتهایی) از REST
    last = max((p["open_time"].max() for p in parts if len(p)), default=None)
    tail_from = (last + STEP).to_pydatetime().replace(tzinfo=timezone.utc) if last is not None else start
    parts.append(fetch_rest(symbol, tail_from, end))
    df = pd.concat([p for p in parts if len(p)], ignore_index=True)
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    lo = pd.Timestamp(start.replace(tzinfo=None))
    return df[df["open_time"] >= lo].reset_index(drop=True)


def gap_report(df: pd.DataFrame) -> str:
    if df.empty:
        return "بدون داده"
    full = pd.date_range(df["open_time"].iloc[0], df["open_time"].iloc[-1], freq="15min")
    missing = full.difference(pd.DatetimeIndex(df["open_time"]))
    if len(missing) == 0:
        return f"{len(df)} کندل، بدون حفره"
    # بزرگ‌ترین حفره‌ی پیوسته
    s = pd.Series(missing)
    grp = (s.diff() != pd.Timedelta(minutes=15)).cumsum()
    big = s.groupby(grp).agg(["first", "count"]).sort_values("count", ascending=False).iloc[0]
    return (f"{len(df)} کندل، {len(missing)} کندل گم‌شده ({len(missing)/len(full)*100:.2f}٪)، "
            f"بزرگ‌ترین حفره: {int(big['count'])} کندل از {big['first']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    sys.path.insert(0, os.getcwd())
    if a.symbols:
        symbols = a.symbols
    else:
        from signal_bot import SYMBOLS as symbols   # همان نمادهای ربات (اجرا از ریشه‌ی ریپو)
    os.makedirs(a.out, exist_ok=True)
    end = datetime.now(timezone.utc)
    start = (end - timedelta(days=a.days)).replace(hour=0, minute=0, second=0, microsecond=0)
    for s in symbols:
        try:
            df = download_symbol(s, start, end)
        except Exception as e:  # noqa: BLE001
            print(f"[{s}] ❌ {e}")
            continue
        df.to_csv(os.path.join(a.out, f"{s}_15m.csv"), index=False)
        print(f"[{s}] {gap_report(df)}")


if __name__ == "__main__":
    main()
