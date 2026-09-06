"""
تست اتصال به صرافی تبدیل - کاملاً read-only، هیچ سفارشی ثبت نمی‌کند.

قبل از فعال‌کردن هرگونه اجرای واقعی، این اسکریپت را اجرا کن تا مطمئن
شوی api_key/api_secret درست کار می‌کنند:

    محلی:
        export TABDEAL_API_KEY="..."
        export TABDEAL_API_SECRET="..."
        python tabdeal_test_connection.py

    گیت‌هاب اکشن: از ورک‌فلوی .github/workflows/tabdeal_test.yml استفاده کن
    (فقط با اجرای دستی/workflow_dispatch، در زمان‌بندی خودکار قرار ندارد).
"""
from __future__ import annotations
from tabdeal_client import TabdealClient, TabdealAPIError
from tabdeal_executor import TABDEAL_API_KEY, TABDEAL_API_SECRET, SYMBOL_MAP


def main():
    print("=== تست اتصال به صرافی تبدیل ===\n")

    client = TabdealClient(api_key=TABDEAL_API_KEY, api_secret=TABDEAL_API_SECRET)

    # ۱) تست پینگ (عمومی، بدون نیاز به کلید)
    try:
        client.ping()
        print("✅ پینگ سرور تبدیل موفق بود.")
    except TabdealAPIError as e:
        print(f"❌ پینگ سرور ناموفق بود: {e}")
        return

    # ۲) زمان سرور
    try:
        t = client.server_time()
        print(f"✅ زمان سرور تبدیل: {t}")
    except TabdealAPIError as e:
        print(f"❌ گرفتن زمان سرور ناموفق بود: {e}")

    # ۳) قیمت لحظه‌ای چند بازار نمونه (عمومی)
    print("\n--- قیمت لحظه‌ای نمونه (order book) ---")
    for binance_sym, tabdeal_sym in list(SYMBOL_MAP.items())[:3]:
        try:
            price = client.get_mid_price(tabdeal_sym)
            print(f"✅ {tabdeal_sym}: ~{price:,.0f} تومان")
        except TabdealAPIError as e:
            print(f"❌ {tabdeal_sym}: خطا -> {e}")
        except (KeyError, IndexError):
            print(f"⚠️  {tabdeal_sym}: این بازار روی تبدیل پیدا نشد یا order book خالی است "
                  f"(احتمالاً نام دقیق نماد را باید از exchange_info تایید کنی).")

    # ۴) تست بخش خصوصی: موجودی حساب (نیاز به api_key/api_secret معتبر)
    print("\n--- تست بخش خصوصی (نیاز به API Key) ---")
    if not TABDEAL_API_KEY or not TABDEAL_API_SECRET:
        print("⚠️  TABDEAL_API_KEY یا TABDEAL_API_SECRET تنظیم نشده - بخش خصوصی رد شد.")
        print("   برای تست کامل، این دو متغیر محیطی را تنظیم کن.")
        return

    try:
        account = client.account()
        print(f"✅ اتصال خصوصی موفق بود. canTrade={account.get('canTrade')}")
        print("\n   موجودی‌های غیرصفر:")
        found_any = False
        for b in account.get("balances", []):
            free = float(b.get("free", 0))
            freeze = float(b.get("freeze", 0))
            if free > 0 or freeze > 0:
                found_any = True
                print(f"   - {b['asset']}: آزاد={free}  فریز={freeze}")
        if not found_any:
            print("   (موجودی غیرصفری پیدا نشد)")
    except TabdealAPIError as e:
        print(f"❌ اتصال خصوصی ناموفق بود: {e}")
        print("   کدهای رایج: 1100/1103 = مشکل کلید یا امضا، 1101/1102 = مشکل timestamp")

    print("\n=== پایان تست - هیچ سفارشی ثبت نشد ===")


if __name__ == "__main__":
    main()
