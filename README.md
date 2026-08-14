
# Trading Journal Telegram Bot — Checklist Edition

این نسخه بر اساس دو تصویر ارسالی ساخته شده است.

## ساختار چک‌لیست
1. Liquidity (4H)
2. Order Block (1H)
3. Market Turning Point (15M)
4. FVG / IFVG
5. Volume Profile / POC
6. Entry (1M)

تمام 20 معیار چک‌لیست به صورت «بله/خیر» ثبت می‌شوند.

## ژورنال
برای هر معامله:
- Symbol
- Buy / Sell
- Entry
- Stop Loss
- Take Profit
- Lot
- Balance before trade
- دلیل ورود
- احساس لحظه ورود
- 20 مورد چک‌لیست
- توضیحات چک‌لیست
- Result: Win / Loss / BE
- P/L
- Final Move Point
- Final R:R
- دلیل خروج
- درس معامله
- Screenshot

## گزارش‌ها
`/today`
- تعداد معاملات
- Long / Short
- Winning / Losing / BE
- Win Rate
- Net P/L
- Total Profit / Loss
- Profit Factor
- Average R:R
- درصد رعایت هر 6 بخش چک‌لیست
- 5 قانون ضعیف‌تر

`/week`
همین گزارش برای 7 روز اخیر + بهترین و بدترین معامله.

`/checklist`
گزارش 30 روز اخیر و مقایسه عملکرد معاملاتی وقتی حداقل 80% چک‌لیست رعایت شده با وقتی کمتر از 80% رعایت شده.

## اجرا

Python 3.10+:

```bash
pip install -r requirements.txt
```

توکن بات را از BotFather بگیر و در متغیر محیطی قرار بده:

Linux/macOS:
```bash
export BOT_TOKEN="YOUR_TOKEN"
python bot.py
```

Windows PowerShell:
```powershell
$env:BOT_TOKEN="YOUR_TOKEN"
python bot.py
```

فایل `journal.db` خودکار ساخته می‌شود.
