"""
پل بین «سیگنال استراتژی» (که قیمت‌هایش بر مبنای جفت‌ارز USDT در بایننس محاسبه
شده) و «سفارش واقعی» روی صرافی تبدیل (که بازارهایش تومانی هستند).

چرا نمی‌توان مستقیم قیمت‌های بایننس را به تبدیل فرستاد؟
    چون قیمت BTCUSDT روی بایننس یک عدد دلاری است، ولی بازار BTCIRT روی
    تبدیل یک عدد تومانی است. این دو قابل جمع/تفریق مستقیم نیستند.

راه‌حل: به‌جای قیمت مطلق، «فاصله‌ی درصدی» حد ضرر و حد سود نسبت به نقطه‌ی
ورود را از سیگنال استخراج می‌کنیم، و همان درصدها را روی قیمت لحظه‌ای
واقعیِ بازار تبدیل (که از خودِ تبدیل گرفته می‌شود، نه از بایننس) پیاده
می‌کنیم. مثال: اگر سیگنال گفته "SL معادل ۲٪ پایین‌تر از ورود"، همان ۲٪
را روی قیمت لحظه‌ای BTCIRT اعمال می‌کنیم.

⚠️ ایمنی: این ماژول به‌صورت پیش‌فرض dry_run=True دارد (یعنی فقط لاگ
می‌کند، سفارش واقعی نمی‌فرستد) و یک سقف سخت (MAX_POSITION_IRT) برای
ارزش هر پوزیشن دارد که حتی با dry_run=False هم رعایت می‌شود.
این ماژول عمداً در هیچ‌کدام از trading_bot.py / trading_bot_v2.py وارد
(import) نشده - یعنی هیچ استراتژی خودکار نمی‌تواند از این‌جا سفارش
واقعی بزند. استفاده از آن فقط دستی و آگاهانه است.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from tabdeal_client import TabdealClient, TabdealAPIError

# =====================================================================
# تنظیمات امنیتی و ایمنی - قبل از خاموش‌کردن dry_run حتماً بخوان
# =====================================================================
DRY_RUN = os.environ.get("TABDEAL_DRY_RUN", "true").lower() != "false"

# سقف سخت هر پوزیشن به تومان - حتی اگر جای دیگری در کد اشتباه محاسبه شود،
# این عدد رعایت می‌شود و مقدار بزرگ‌تر رد (raise) می‌شود.
MAX_POSITION_IRT = float(os.environ.get("TABDEAL_MAX_POSITION_IRT", "200000"))

TABDEAL_API_KEY = os.environ.get("TABDEAL_API_KEY", "")
TABDEAL_API_SECRET = os.environ.get("TABDEAL_API_SECRET", "")

# نگاشت نماد بایننس (که سیگنال از آن می‌آید) -> نماد بازار روی تبدیل
# فقط بازارهایی که واقعاً روی تبدیل معامله می‌شوند را اضافه کن.
# نکته: مقادیر را قبل از استفاده‌ی واقعی از طریق client.exchange_info() تایید کن؛
# نام دقیق نمادها ممکن است در تبدیل کمی متفاوت باشد (مثلاً بدون آندرلاین).
SYMBOL_MAP = {
    "BTCUSDT": "BTCIRT",
    "ETHUSDT": "ETHIRT",
    "BNBUSDT": "BNBIRT",
    "DOGEUSDT": "DOGEIRT",
    "SOLUSDT": "SOLIRT",
}


class SafetyLimitError(Exception):
    """وقتی محاسبه‌ای بخواهد از سقف ایمنی MAX_POSITION_IRT عبور کند، این خطا بالا می‌آید."""


@dataclass
class ExecutionPlan:
    binance_symbol: str
    tabdeal_symbol: str
    side: str                  # "BUY" یا "SELL"
    quantity: float
    entry_price_irt: float
    stop_loss_irt: float
    take_profit_irt: float
    notional_irt: float
    dry_run: bool


def get_client() -> TabdealClient:
    return TabdealClient(api_key=TABDEAL_API_KEY, api_secret=TABDEAL_API_SECRET)


def build_execution_plan(client: TabdealClient, binance_symbol: str, direction: str,
                          signal_entry: float, signal_sl: float, signal_tp: float,
                          max_position_irt: float = MAX_POSITION_IRT) -> ExecutionPlan:
    """
    از یک سیگنال (که قیمت‌هایش دلاری/USDT است) یک برنامه‌ی اجرای واقعی
    تومانی روی تبدیل می‌سازد - بدون ارسال هیچ سفارشی.
    direction: "long" یا "short"
    """
    if binance_symbol not in SYMBOL_MAP:
        raise ValueError(f"نماد {binance_symbol} در SYMBOL_MAP تعریف نشده - اول نگاشتش را اضافه کن.")
    tabdeal_symbol = SYMBOL_MAP[binance_symbol]

    if signal_entry <= 0:
        raise ValueError("قیمت ورود سیگنال باید مثبت باشد.")

    risk_pct = abs(signal_entry - signal_sl) / signal_entry
    reward_pct = abs(signal_tp - signal_entry) / signal_entry

    # قیمت لحظه‌ای واقعی از خودِ تبدیل (نه بایننس)
    entry_irt = client.get_mid_price(tabdeal_symbol)

    if direction == "long":
        sl_irt = entry_irt * (1 - risk_pct)
        tp_irt = entry_irt * (1 + reward_pct)
        side = "BUY"
    elif direction == "short":
        sl_irt = entry_irt * (1 + risk_pct)
        tp_irt = entry_irt * (1 - reward_pct)
        side = "SELL"
    else:
        raise ValueError(f"direction نامعتبر: {direction}")

    notional_irt = min(max_position_irt, MAX_POSITION_IRT)
    if notional_irt > MAX_POSITION_IRT:
        # این شرط عملاً هرگز رخ نمی‌دهد (چون بالا min گرفتیم) ولی به‌عنوان
        # لایه‌ی دومِ ایمنی نگه داشته می‌شود تا اگر تابع در آینده تغییر کرد
        # هم سقف هرگز دور زده نشود.
        raise SafetyLimitError(
            f"ارزش پوزیشن محاسبه‌شده ({notional_irt:,.0f} تومان) از سقف مجاز "
            f"({MAX_POSITION_IRT:,.0f} تومان) بیشتر است."
        )

    quantity = notional_irt / entry_irt

    return ExecutionPlan(
        binance_symbol=binance_symbol,
        tabdeal_symbol=tabdeal_symbol,
        side=side,
        quantity=round(quantity, 6),
        entry_price_irt=entry_irt,
        stop_loss_irt=round(sl_irt, 0),
        take_profit_irt=round(tp_irt, 0),
        notional_irt=notional_irt,
        dry_run=DRY_RUN,
    )


def describe_plan(plan: ExecutionPlan) -> str:
    return (
        f"[{'DRY-RUN' if plan.dry_run else 'REAL'}] {plan.side} {plan.tabdeal_symbol} "
        f"(از سیگنال {plan.binance_symbol})\n"
        f"  مقدار: {plan.quantity}\n"
        f"  ارزش پوزیشن: {plan.notional_irt:,.0f} تومان\n"
        f"  ورود لحظه‌ای: {plan.entry_price_irt:,.0f} تومان\n"
        f"  حد ضرر: {plan.stop_loss_irt:,.0f} تومان\n"
        f"  حد سود: {plan.take_profit_irt:,.0f} تومان"
    )


def execute_plan(client: TabdealClient, plan: ExecutionPlan) -> dict | None:
    """
    اجرای واقعی: یک سفارش MARKET برای ورود + یک سفارش OCO برای خروج
    (حد سود و حد ضرر هم‌زمان). اگر plan.dry_run=True باشد، هیچ درخواستی
    به تبدیل فرستاده نمی‌شود و فقط توضیح برنامه چاپ می‌شود.
    """
    print(describe_plan(plan))

    if plan.notional_irt > MAX_POSITION_IRT:
        raise SafetyLimitError("اجرای این پلن رد شد: از سقف ایمنی MAX_POSITION_IRT عبور می‌کند.")

    if plan.dry_run:
        print(">>> TABDEAL_DRY_RUN فعال است؛ هیچ سفارش واقعی ارسال نشد.")
        return None

    entry_order = client.new_order(
        symbol=plan.tabdeal_symbol,
        side=plan.side,
        order_type="MARKET",
        quantity=plan.quantity,
    )
    print("سفارش ورود ثبت شد:", entry_order)

    exit_side = "SELL" if plan.side == "BUY" else "BUY"
    oco = client.new_oco_order(
        symbol=plan.tabdeal_symbol,
        side=exit_side,
        quantity=plan.quantity,
        price=plan.take_profit_irt,
        stop_price=plan.stop_loss_irt,
        stop_limit_price=plan.stop_loss_irt,
    )
    print("سفارش OCO (حد سود+حد ضرر) ثبت شد:", oco)

    return {"entry_order": entry_order, "oco_order": oco}


if __name__ == "__main__":
    print("این فایل برای import شدن طراحی شده؛ برای تست اتصال از")
    print("tabdeal_test_connection.py استفاده کن.")
