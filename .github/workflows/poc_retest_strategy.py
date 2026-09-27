"""
استراتژی: POC Retest (بازگشت به نقطه‌ی حجم غالب بعد از شکست)
================================================================

خلاصه‌ی چیزی که در ویدیو دیده شد:
1) قیمت یک حرکت ایمپالسیو (صعودی) از یک ناحیه‌ی رنج/تثبیت (کف) به سمت بالا (سقف) می‌زند.
2) روی همین لگ (leg) پروفایل حجم (Volume Profile) کشیده می‌شود و خط POC
   (Point of Control = قیمتی که بیشترین حجم معامله در آن رد و بدل شده) پیدا می‌شود.
3) بعد از شکست، قیمت برمی‌گردد و POC را دوباره تست می‌کند.
4) ناحیه‌ی بالای POC = ناحیه‌ی ادامه‌ی روند (سبز/آبی در ویدیو)
   ناحیه‌ی زیر POC = ناحیه‌ی رد شدن/استاپ (صورتی در ویدیو)
5) اگر کندلی سایه‌اش وارد ناحیه‌ی زیر POC بشود ولی بسته شدنش دوباره بالای POC باشد
   (کندل رد کننده / Rejection Candle روی POC) → سیگنال ورود به پوزیشن لانگ صادر می‌شود.
6) حد ضرر: کمی پایین‌تر از کف کندل رد کننده (یا پایین‌تر از POC).
7) حد سود: سقف قبلی (ceiling) یا با نسبت ریسک به ریوارد مشخص (RR).

برای حالت نزولی (شورت) دقیقاً برعکس این منطق اجرا می‌شود: لگ نزولی → POC آن لگ →
بازگشت به POC از پایین → کندل رد کننده که سایه‌اش وارد بالای POC می‌شود ولی بسته‌ی آن
زیر POC می‌ماند → سیگنال شورت.

این فایل مستقل (self-contained) نوشته شده تا هم قابل تست تکی باشد و هم به راحتی
به موتور بک‌تست/سیگنال‌دهی ریپوهای دیگرتان (crypto-strategy-lab / Crypto-Signal-Bot)
وصل شود: فقط کافیست خروجی generate_signals را با ساختار سیگنال همان پروژه هماهنگ کنید.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------
# ساختار داده‌ی سیگنال
# ----------------------------------------------------------------------------
@dataclass
class Signal:
    time: pd.Timestamp
    direction: str          # "long" یا "short"
    entry: float
    stop_loss: float
    take_profit: float
    poc: float
    leg_start: pd.Timestamp
    leg_end: pd.Timestamp
    reason: str = "poc_retest_rejection"

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward(self) -> float:
        return abs(self.take_profit - self.entry)

    @property
    def rr(self) -> float:
        return self.reward / self.risk if self.risk else float("nan")


def validate_signal(sig: Signal, min_reward_pct: float = 0.003) -> bool:
    """
    گارد ایمنی (شبیه validate_signal در crypto-strategy-lab): مطمئن می‌شویم
    TP/SL سمت درستِ entry هستند و فاصله‌ی سود حداقلیِ منطقی وجود دارد،
    تا سیگنال‌های بی‌فایده یا با جهت اشتباه ساخته نشوند.
    """
    if sig.risk <= 0 or sig.reward <= 0:
        return False
    if sig.direction == "long":
        if not (sig.stop_loss < sig.entry < sig.take_profit):
            return False
    else:
        if not (sig.take_profit < sig.entry < sig.stop_loss):
            return False
    if (sig.reward / sig.entry) < min_reward_pct:
        return False
    return True


# ----------------------------------------------------------------------------
# استراتژی
# ----------------------------------------------------------------------------
@dataclass
class POCRetestStrategy:
    pivot_left: int = 3          # تعداد کندل چپ برای تشخیص سوینگ
    pivot_right: int = 3         # تعداد کندل راست برای تشخیص سوینگ
    vp_bins: int = 24            # تعداد باکت‌های قیمتی برای پروفایل حجم
    value_area_pct: float = 0.70  # درصد استاندارد ناحیه ارزش (Value Area)
    sl_buffer_pct: float = 0.001  # بافر امنیتی زیر/بالای کندل رد کننده برای SL
    rr_ratio: float = 2.0         # نسبت ریسک به ریوارد پیش‌فرض اگر از سقف/کف قبلی استفاده نشود
    use_prior_extreme_as_tp: bool = True  # TP = سقف/کف قبلیِ لگ، در غیر این صورت TP بر اساس rr_ratio
    min_reward_pct: float = 0.003

    # -------------------------------------------------------------- pivots --
    def _find_pivots(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        تشخیص سوینگ‌های فرکتالی (swing high / swing low) با مقایسه‌ی هر کندل
        با N کندل چپ و N کندل راستش.
        """
        n_l, n_r = self.pivot_left, self.pivot_right
        highs, lows = df["high"].values, df["low"].values
        is_high = np.zeros(len(df), dtype=bool)
        is_low = np.zeros(len(df), dtype=bool)

        for i in range(n_l, len(df) - n_r):
            window_h = highs[i - n_l:i + n_r + 1]
            window_l = lows[i - n_l:i + n_r + 1]
            if highs[i] == window_h.max() and np.argmax(window_h) == n_l:
                is_high[i] = True
            if lows[i] == window_l.min() and np.argmin(window_l) == n_l:
                is_low[i] = True

        out = df.copy()
        out["swing_high"] = is_high
        out["swing_low"] = is_low
        return out

    # ---------------------------------------------------------- volume profile --
    def _volume_profile(self, leg: pd.DataFrame) -> Optional[dict]:
        """
        ساخت پروفایل حجم روی یک لگ (leg) از کندل‌ها:
        - بازه‌ی قیمتی low..high آن لگ به vp_bins باکت تقسیم می‌شود.
        - حجم هر کندل به‌صورت مساوی بین باکت‌هایی که در محدوده‌ی
          low..high آن کندل قرار می‌گیرند پخش می‌شود (روش ساده‌ی تقریبی).
        - POC = مرکز باکتی که بیشترین حجم را جمع کرده.
        """
        if leg.empty:
            return None

        lo, hi = leg["low"].min(), leg["high"].max()
        if hi <= lo:
            return None

        edges = np.linspace(lo, hi, self.vp_bins + 1)
        vol_per_bin = np.zeros(self.vp_bins)

        for _, row in leg.iterrows():
            c_lo, c_hi, c_vol = row["low"], row["high"], row["volume"]
            if c_hi <= c_lo or c_vol <= 0:
                continue
            # باکت‌هایی که کندل با آن‌ها همپوشانی دارد
            lo_idx = np.searchsorted(edges, c_lo, side="right") - 1
            hi_idx = np.searchsorted(edges, c_hi, side="right") - 1
            lo_idx = max(lo_idx, 0)
            hi_idx = min(hi_idx, self.vp_bins - 1)
            span = hi_idx - lo_idx + 1
            if span <= 0:
                continue
            vol_per_bin[lo_idx:hi_idx + 1] += c_vol / span

        poc_idx = int(np.argmax(vol_per_bin))
        poc_price = (edges[poc_idx] + edges[poc_idx + 1]) / 2.0

        # ناحیه ارزش (Value Area): از POC به بیرون گسترش می‌دهیم تا
        # value_area_pct از کل حجم پوشش داده شود (برای مصارف بعدی/نمایش).
        total_vol = vol_per_bin.sum()
        target = total_vol * self.value_area_pct
        lo_i = hi_i = poc_idx
        covered = vol_per_bin[poc_idx]
        while covered < target and (lo_i > 0 or hi_i < self.vp_bins - 1):
            expand_lo = vol_per_bin[lo_i - 1] if lo_i > 0 else -1
            expand_hi = vol_per_bin[hi_i + 1] if hi_i < self.vp_bins - 1 else -1
            if expand_hi >= expand_lo:
                hi_i += 1
                covered += vol_per_bin[hi_i]
            else:
                lo_i -= 1
                covered += vol_per_bin[lo_i]

        return {
            "poc": poc_price,
            "val": edges[lo_i],       # Value Area Low
            "vah": edges[hi_i + 1],   # Value Area High
        }

    # --------------------------------------------------------------- main --
    def generate_signals(self, df: pd.DataFrame) -> List[Signal]:
        """
        df باید ستون‌های: time (یا index زمانی), open, high, low, close, volume را داشته باشد.
        خروجی: لیستی از Signal برای هر ستاپ POC-retest معتبر که پیدا شده.
        """
        df = df.reset_index(drop=False)
        if "time" not in df.columns:
            df = df.rename(columns={df.columns[0]: "time"})

        piv = self._find_pivots(df)
        swing_idx = piv.index[(piv["swing_high"]) | (piv["swing_low"])].tolist()

        signals: List[Signal] = []

        for k in range(1, len(swing_idx)):
            start_i, end_i = swing_idx[k - 1], swing_idx[k]
            if end_i - start_i < 3:
                continue

            leg = piv.iloc[start_i:end_i + 1]
            is_up_leg = piv.loc[start_i, "swing_low"] and piv.loc[end_i, "swing_high"]
            is_down_leg = piv.loc[start_i, "swing_high"] and piv.loc[end_i, "swing_low"]
            if not (is_up_leg or is_down_leg):
                continue

            vp = self._volume_profile(leg)
            if vp is None:
                continue
            poc = vp["poc"]

            direction = "long" if is_up_leg else "short"
            leg_extreme = piv.loc[end_i, "high"] if is_up_leg else piv.loc[end_i, "low"]

            # به دنبال کندل رد کننده (rejection) روی POC بعد از پایان لگ می‌گردیم
            search_zone = piv.iloc[end_i + 1: end_i + 1 + 30]  # پنجره‌ی جست‌وجوی معقول بعد از سقف/کف
            for _, row in search_zone.iterrows():
                if direction == "long":
                    touched = row["low"] <= poc
                    rejected = row["close"] > poc
                else:
                    touched = row["high"] >= poc
                    rejected = row["close"] < poc

                if touched and rejected:
                    entry = row["close"]
                    if direction == "long":
                        sl = min(row["low"], poc) * (1 - self.sl_buffer_pct)
                        tp = leg_extreme if self.use_prior_extreme_as_tp else entry + self.rr_ratio * (entry - sl)
                    else:
                        sl = max(row["high"], poc) * (1 + self.sl_buffer_pct)
                        tp = leg_extreme if self.use_prior_extreme_as_tp else entry - self.rr_ratio * (sl - entry)

                    sig = Signal(
                        time=row["time"],
                        direction=direction,
                        entry=entry,
                        stop_loss=sl,
                        take_profit=tp,
                        poc=poc,
                        leg_start=piv.loc[start_i, "time"],
                        leg_end=piv.loc[end_i, "time"],
                    )
                    if validate_signal(sig, self.min_reward_pct):
                        signals.append(sig)
                    break  # فقط اولین کندل رد کننده‌ی معتبر بعد از هر لگ در نظر گرفته می‌شود

        return signals


# ----------------------------------------------------------------------------
# بک‌تست ساده (شبیه‌سازی next_open + بررسی برخورد TP/SL روی کندل‌های بعدی)
# ----------------------------------------------------------------------------
def backtest(df: pd.DataFrame, signals: List[Signal]) -> pd.DataFrame:
    df = df.reset_index(drop=False)
    if "time" not in df.columns:
        df = df.rename(columns={df.columns[0]: "time"})

    results = []
    for sig in signals:
        future = df[df["time"] > sig.time]
        outcome, exit_price, exit_time = "open", None, None

        for _, row in future.iterrows():
            if sig.direction == "long":
                hit_sl = row["low"] <= sig.stop_loss
                hit_tp = row["high"] >= sig.take_profit
            else:
                hit_sl = row["high"] >= sig.stop_loss
                hit_tp = row["low"] <= sig.take_profit

            if hit_sl and hit_tp:
                # هر دو در یک کندل: محافظه‌کارانه فرض می‌کنیم SL اول خورده
                outcome, exit_price, exit_time = "sl", sig.stop_loss, row["time"]
                break
            elif hit_sl:
                outcome, exit_price, exit_time = "sl", sig.stop_loss, row["time"]
                break
            elif hit_tp:
                outcome, exit_price, exit_time = "tp", sig.take_profit, row["time"]
                break

        pnl_pct = None
        if exit_price is not None:
            pnl_pct = ((exit_price - sig.entry) / sig.entry) if sig.direction == "long" \
                else ((sig.entry - exit_price) / sig.entry)

        results.append({
            "time": sig.time, "direction": sig.direction, "entry": sig.entry,
            "sl": sig.stop_loss, "tp": sig.take_profit, "poc": sig.poc,
            "outcome": outcome, "exit_time": exit_time, "pnl_pct": pnl_pct,
            "rr": sig.rr,
        })

    return pd.DataFrame(results)
