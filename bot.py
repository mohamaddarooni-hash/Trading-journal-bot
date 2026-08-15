
import os
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler, CallbackQueryHandler,
    ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
DB = "journal.db"

# Trade information
SYMBOL, SIDE, ENTRY, SL, TP, LOT, REASON, EMOTION = range(8)

# Checklist questions
CHECKLIST_START = 8
CHECKLIST_END = 26

CHECKLIST = [
    ("1. LIQUIDITY (4H)", [
        "ناحیه نقدینگی در 4 ساعت گذشته مشخص شد.",
        "بر اساس فلو، ناحیه مورد نظر شناسایی شد."
    ]),
    ("2. ORDER BLOCK (1H)", [
        "در محدوده نقدینگی، اردربلاک پیدا شد.",
        "بازگشت قیمت با سناریوی حرکتی/جهتی هماهنگ است."
    ]),
    ("3. MARKET TURNING POINT (15M)", [
        "تشکیل بازار (CRT) تأیید شد.",
        "تشکیل CHoCH تأیید شد.",
        "تشکیل MSS تأیید شد.",
        "تشکیل EBP تأیید شد.",
        "نقطه چرخش معتبر است."
    ]),
    ("4. FVG / IFVG", [
        "گپ ارزش منصفانه (FVG) در جهت معامله وجود دارد.",
        "با IFVG معتبر، تأیید شده است.",
        "قیمت به ساختار بازار احترام گذاشته است."
    ]),
    ("5. VOLUME PROFILE / POC", [
        "POC با حجم زیاد مشخص است.",
        "POC در 4 ساعت گذشته مشخص شده است.",
        "قیمت نسبت به POC واکنش نشان داده است.",
        "تأیید سناریو توسط POC صورت گرفته است."
    ]),
    ("6. ENTRY (1M)", [
        "قیمت به محدوده اردربلاک تایم 1M برگشته است.",
        "اردربلاک با POC منطقی/هم‌راستا است.",
        "ریسک به ریوارد مناسب است.",
        "ورود پس از دریافت تأیید انجام شد."
    ])
]

CHECK_ITEMS = [(section, q) for section, qs in CHECKLIST for q in qs]
TOTAL_CHECKS = len(CHECK_ITEMS)

# Conversation states after the checklist
CHECK_NOTES = CHECKLIST_START + TOTAL_CHECKS
RESULT = CHECK_NOTES + 1
PNL = RESULT + 1
FINAL_MOVE = PNL + 1
FINAL_RR = FINAL_MOVE + 1
EXIT_REASON = FINAL_RR + 1
LESSONS = EXIT_REASON + 1
BALANCE = LESSONS + 1

def db():
    conn = sqlite3.connect(DB)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        symbol TEXT, side TEXT, entry REAL, sl REAL, tp REAL, lot REAL,
        balance_before REAL, reason TEXT, emotion TEXT,
        checklist TEXT NOT NULL,
        checklist_notes TEXT,
        result TEXT, pnl REAL, final_move TEXT, final_rr REAL,
        exit_reason TEXT, lessons TEXT,
        screenshot_file_id TEXT
    )
    """)
    conn.commit()
    return conn

def pct(n, d):
    return 0 if not d else n / d * 100

def main_keyboard():
    return ReplyKeyboardMarkup(
        [["📝 معامله جدید"], ["📊 امروز", "📈 این هفته"], ["📋 آمار چک‌لیست"]],
        resize_keyboard=True
    )

async def start(update, context):
    await update.message.reply_text(
        "📒 ژورنال معاملاتی آماده است.\n\n"
        "این نسخه دقیقاً بر اساس چک‌لیست تصویری تو ساخته شده:\n"
        "Liquidity → OB → Turning Point → FVG/IFVG → POC → Entry",
        reply_markup=main_keyboard()
    )

async def help_cmd(update, context):
    await update.message.reply_text(
        "/new — معامله جدید\n"
        "/today — گزارش امروز\n"
        "/week — گزارش ۷ روز اخیر\n"
        "/checklist — آمار رعایت چک‌لیست\n"
        "/cancel — لغو"
    )

async def new_trade(update, context):
    context.user_data.clear()
    context.user_data["check_idx"] = 0
    context.user_data["checks"] = []
    await update.message.reply_text(
        "📝 ثبت معامله جدید\n\n1/9 — نماد معاملاتی؟\nمثلاً XAUUSD",
        reply_markup=ReplyKeyboardRemove()
    )
    return SYMBOL

async def symbol(update, context):
    context.user_data["symbol"] = update.message.text.strip().upper()
    await update.message.reply_text("2/9 — جهت معامله؟ Buy یا Sell")
    return SIDE

async def side(update, context):
    context.user_data["side"] = update.message.text.strip()
    await update.message.reply_text("3/9 — نقطه ورود؟")
    return ENTRY

async def number_field(update, context, key, prompt, next_state):
    try:
        value = float(update.message.text.replace(",", "."))
        context.user_data[key] = value
        await update.message.reply_text(prompt)
        return next_state
    except ValueError:
        await update.message.reply_text("لطفاً فقط عدد وارد کن.")
        return next_state - 1

async def entry(update, context):
    return await number_field(update, context, "entry", "4/9 — Stop Loss؟", SL)

async def sl(update, context):
    return await number_field(update, context, "sl", "5/9 — Take Profit؟", TP)

async def tp(update, context):
    return await number_field(update, context, "tp", "6/9 — حجم معامله؟\nمثلاً 0.01", LOT)

async def lot(update, context):
    return await number_field(update, context, "lot", "7/9 — دلیل اصلی ورود چه بود؟", REASON)

async def reason(update, context):
    context.user_data["reason"] = update.message.text.strip()
    await update.message.reply_text(
        "8/9 — احساس لحظه ورود؟\n"
        "آرام و منطقی / بی‌حوصله و عجله‌زده / خشمگین / انتقامی"
    )
    return EMOTION

async def emotion(update, context):
    context.user_data["emotion"] = update.message.text.strip()
    await update.message.reply_text("9/9 — موجودی/بالانس قبل از معامله؟")
    return BALANCE

async def balance(update, context):
    try:
        context.user_data["balance_before"] = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("فقط عدد وارد کن؛ مثلاً 601")
        return BALANCE
    context.user_data["check_idx"] = 0
    await ask_check(update, context)
    return CHECKLIST_START

async def ask_check(update, context):
    i = context.user_data["check_idx"]
    section, question = CHECK_ITEMS[i]
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 بله", callback_data="check_yes"),
            InlineKeyboardButton("🔴 خیر", callback_data="check_no"),
        ]
    ])
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"☑️ چک‌لیست {i+1}/{TOTAL_CHECKS}\n"
            f"📌 {section}\n\n{question}\n\n"
            "رعایت شده؟"
        ),
        reply_markup=keyboard
    )

async def checklist_answer(update, context):
    # Fallback for typed answers.
    answer = update.message.text.strip().lower()
    if answer not in ("بله", "خیر", "yes", "no"):
        await update.message.reply_text("لطفاً فقط گزینه «بله» یا «خیر» را بزن.")
        return CHECKLIST_START + context.user_data.get("check_idx", 0)

    context.user_data["checks"].append(answer in ("بله", "yes"))
    context.user_data["check_idx"] += 1
    if context.user_data["check_idx"] < TOTAL_CHECKS:
        await ask_check(update, context)
        return CHECKLIST_START + context.user_data["check_idx"]

    score = sum(context.user_data["checks"])
    await update.message.reply_text(
        f"✅ چک‌لیست تمام شد. امتیاز: {score}/{TOTAL_CHECKS} "
        f"({pct(score, TOTAL_CHECKS):.1f}%)\n\n"
        "اگر برای چک‌لیست توضیحی داری بنویس؛ اگر نداری «ندارم»."
    )
    return CHECK_NOTES

async def checklist_button(update, context):
    query = update.callback_query
    await query.answer()

    if query.data not in ("check_yes", "check_no"):
        return CHECKLIST_START + context.user_data.get("check_idx", 0)

    context.user_data["checks"].append(query.data == "check_yes")
    context.user_data["check_idx"] += 1

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if context.user_data["check_idx"] < TOTAL_CHECKS:
        await ask_check(update, context)
        return CHECKLIST_START + context.user_data["check_idx"]

    score = sum(context.user_data["checks"])
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✅ چک‌لیست تمام شد. امتیاز: {score}/{TOTAL_CHECKS} "
            f"({pct(score, TOTAL_CHECKS):.1f}%)\n\n"
            "اگر برای چک‌لیست توضیحی داری بنویس؛ اگر نداری «ندارم»."
        )
    )
    return CHECK_NOTES

async def check_notes(update, context):
    context.user_data["checklist_notes"] = update.message.text.strip()
    await update.message.reply_text("نتیجه معامله؟ Win / Loss / BE")
    return RESULT

async def result(update, context):
    context.user_data["result"] = update.message.text.strip().upper()
    await update.message.reply_text("P/L معامله به دلار؟\nمثلاً +12.5 یا -5")
    return PNL

async def pnl(update, context):
    try:
        context.user_data["pnl"] = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("فقط عدد وارد کن؛ مثلاً -5.50")
        return PNL

    # Calculate planned R:R from entry, SL and TP
    e, sl, tp = context.user_data["entry"], context.user_data["sl"], context.user_data["tp"]
    risk = abs(e - sl)
    reward = abs(tp - e)
    rr = reward / risk if risk else 0
    context.user_data["planned_rr"] = rr

    await update.message.reply_text("نقطه نهایی حرکت/خروج قیمت کجا بود؟")
    return FINAL_MOVE

async def final_move(update, context):
    context.user_data["final_move"] = update.message.text.strip()
    await update.message.reply_text(
        f"R:R برنامه‌ریزی‌شده: 1:{context.user_data['planned_rr']:.2f}\n"
        "R:R نهایی معامله را وارد کن؛ مثلاً 2 یا 1.5"
    )
    return FINAL_RR

async def final_rr(update, context):
    try:
        context.user_data["final_rr"] = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("فقط عدد وارد کن؛ مثلاً 2 یا 1.5")
        return FINAL_RR
    await update.message.reply_text("دلیل خروج از معامله چه بود؟")
    return EXIT_REASON

async def exit_reason(update, context):
    context.user_data["exit_reason"] = update.message.text.strip()
    await update.message.reply_text("مهم‌ترین درس این معامله چه بود؟")
    return LESSONS

async def lessons(update, context):
    context.user_data["lessons"] = update.message.text.strip()
    await update.message.reply_text(
        "📸 اگر اسکرین‌شات چارت را داری بفرست؛ اگر نداری «ندارم»."
    )
    # Reuse a temporary state after LESSONS via custom handler isn't convenient,
    # so store without screenshot if text, or handle photo in same state.
    if update.message.photo:
        await save_trade(update, context, update.message.photo[-1].file_id)
        return ConversationHandler.END
    if update.message.text.strip() == "ندارم":
        await save_trade(update, context, None)
        return ConversationHandler.END
    context.user_data["waiting_photo"] = True
    return LESSONS

async def lesson_or_photo(update, context):
    if update.message.photo:
        await save_trade(update, context, update.message.photo[-1].file_id)
        return ConversationHandler.END
    return await lessons(update, context)

async def save_trade(update, context, file_id):
    d = context.user_data
    conn = db()
    conn.execute("""
        INSERT INTO trades
        (user_id, created_at, symbol, side, entry, sl, tp, lot, balance_before, reason,
         emotion, checklist, checklist_notes, result, pnl, final_move, final_rr,
         exit_reason, lessons, screenshot_file_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        update.effective_user.id, datetime.now().isoformat(timespec="seconds"),
        d["symbol"], d["side"], d["entry"], d["sl"], d["tp"], d["lot"],
        d["balance_before"], d["reason"], d["emotion"], ",".join("1" if x else "0" for x in d["checks"]),
        d["checklist_notes"], d["result"], d["pnl"], d["final_move"], d["final_rr"],
        d["exit_reason"], d["lessons"], file_id
    ))
    conn.commit()
    conn.close()

    score = sum(d["checks"])
    await update.message.reply_text(
        f"✅ معامله ثبت شد.\n\n"
        f"📌 {d['symbol']} | {d['side']}\n"
        f"💰 P/L: {d['pnl']:+.2f}$\n"
        f"🎯 نتیجه: {d['result']}\n"
        f"☑️ رعایت چک‌لیست: {score}/{TOTAL_CHECKS} ({pct(score,TOTAL_CHECKS):.1f}%)\n"
        f"📐 R:R نهایی: 1:{d['final_rr']:.2f}\n\n"
        "برای گزارش /today یا /week را بزن.",
        reply_markup=main_keyboard()
    )
    context.user_data.clear()

def fetch_rows(user_id, days):
    since = datetime.now() - timedelta(days=days)
    conn = db()
    rows = conn.execute("""
        SELECT * FROM trades WHERE user_id=? AND created_at>=?
        ORDER BY created_at ASC
    """, (user_id, since.isoformat(timespec="seconds"))).fetchall()
    conn.close()
    return rows

def stats(rows):
    total = len(rows)
    wins = sum(1 for r in rows if str(r[14]).strip().upper() == "WIN")
    losses = sum(1 for r in rows if str(r[14]).strip().upper() == "LOSS")
    be = sum(1 for r in rows if str(r[14]).strip().upper() in ("BE", "BREAK EVEN", "BREAKEVEN"))
    pnl = sum(float(r[15] or 0) for r in rows)
    profit = sum(float(r[15] or 0) for r in rows if float(r[15] or 0) > 0)
    loss = sum(float(r[15] or 0) for r in rows if float(r[15] or 0) < 0)
    rr = sum(float(r[17] or 0) for r in rows) / total if total else 0
    pf = profit / abs(loss) if loss else 0
    return total, wins, losses, be, pnl, profit, loss, rr, pf

def checklist_stats(rows):
    n = len(rows)
    item_rates = []
    section_stats = []
    offset = 0

    for section, questions in CHECKLIST:
        vals = []
        for _ in questions:
            yes = sum(1 for r in rows if r[12].split(",")[offset] == "1") if n else 0
            vals.append(yes)
            offset += 1
        section_yes = sum(vals)
        section_total = n * len(questions)
        section_stats.append((section, section_yes, section_total))
    return section_stats

def report_text(rows, title):
    if not rows:
        return f"📊 {title}\n\nهیچ معامله‌ای ثبت نشده."

    total, wins, losses, be, pnl, profit, loss, rr, pf = stats(rows)
    lines = [
        f"📊 {title}",
        "",
        f"تعداد معاملات: {total}",
        f"🟢 Long: {sum(1 for r in rows if str(r[4]).lower() == 'buy')}",
        f"🔴 Short: {sum(1 for r in rows if str(r[4]).lower() == 'sell')}",
        f"✅ Winning: {wins}",
        f"❌ Losing: {losses}",
        f"➖ BE: {be}",
        f"🎯 Win Rate: {pct(wins,total):.1f}%",
        f"💰 Net P/L: {pnl:+.2f}$",
        f"📈 Total Profit: {profit:+.2f}$",
        f"📉 Total Loss: {loss:+.2f}$",
        f"⚖️ Profit Factor: {pf:.2f}",
        f"📐 Average R:R: 1:{rr:.2f}",
        "",
        "☑️ عملکرد چک‌لیست:"
    ]

    sections = checklist_stats(rows)
    for section, yes, total_checks in sections:
        lines.append(f"• {section}: {pct(yes,total_checks):.1f}%")

    # Individual rules
    lines += ["", "🔎 ضعیف‌ترین قوانین:"]
    item_data = []
    for idx, (section, q) in enumerate(CHECK_ITEMS):
        yes = sum(1 for r in rows if r[12].split(",")[idx] == "1")
        item_data.append((pct(yes, len(rows)), section, q))
    for rate, section, q in sorted(item_data)[:5]:
        lines.append(f"• {rate:.0f}% — {q}")

    # Compliance vs performance
    high = [r for r in rows if pct(sum(x=="1" for x in r[12].split(",")), TOTAL_CHECKS) >= 80]
    low = [r for r in rows if pct(sum(x=="1" for x in r[12].split(",")), TOTAL_CHECKS) < 80]
    if high:
        hw = sum(1 for r in high if str(r[14]).strip().upper()=="WIN")
        lines += ["", f"🧠 وقتی چک‌لیست ≥80٪ رعایت شده: {len(high)} معامله | Win Rate {pct(hw,len(high)):.1f}% | P/L {sum(float(r[15] or 0) for r in high):+.2f}$"]
    if low:
        lw = sum(1 for r in low if str(r[14]).strip().upper()=="WIN")
        lines.append(f"⚠️ وقتی چک‌لیست <80٪ رعایت شده: {len(low)} معامله | Win Rate {pct(lw,len(low)):.1f}% | P/L {sum(float(r[15] or 0) for r in low):+.2f}$")

    return "\n".join(lines)

async def today(update, context):
    await update.message.reply_text(report_text(fetch_rows(update.effective_user.id, 1), "گزارش امروز"))

async def week(update, context):
    rows = fetch_rows(update.effective_user.id, 7)
    text = report_text(rows, "گزارش هفتگی")
    await update.message.reply_text(text)

    if rows:
        # Weekly review prompts
        best = max(rows, key=lambda r: float(r[15] or 0))
        worst = min(rows, key=lambda r: float(r[15] or 0))
        await update.message.reply_text(
            "📝 Weekly Review\n\n"
            f"🏆 بهترین معامله: {best[2]} | {float(best[15] or 0):+.2f}$\n"
            f"⚠️ بدترین معامله: {worst[2]} | {float(worst[15] or 0):+.2f}$\n\n"
            "برای بررسی عمیق‌تر، /checklist را بزن."
        )

async def checklist_report(update, context):
    rows = fetch_rows(update.effective_user.id, 30)
    await update.message.reply_text(report_text(rows, "آمار چک‌لیست — ۳۰ روز اخیر"))

async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("ثبت معامله لغو شد.", reply_markup=main_keyboard())
    return ConversationHandler.END

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    db()
    app = Application.builder().token(TOKEN).build()

    states = {
        SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, symbol)],
        SIDE: [MessageHandler(filters.TEXT & ~filters.COMMAND, side)],
        ENTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, entry)],
        SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, sl)],
        TP: [MessageHandler(filters.TEXT & ~filters.COMMAND, tp)],
        LOT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lot)],
        REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, reason)],
        EMOTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, emotion)],
    BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, balance)],
    }

    # Each checklist item gets its own state, but the same handler processes them.
    for i in range(TOTAL_CHECKS):
        states[CHECKLIST_START+i] = [
            CallbackQueryHandler(checklist_button, pattern="^check_(yes|no)$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, checklist_answer),
        ]

    states[CHECK_NOTES] = [MessageHandler(filters.TEXT & ~filters.COMMAND, check_notes)]
    states[RESULT] = [MessageHandler(filters.TEXT & ~filters.COMMAND, result)]
    states[PNL] = [MessageHandler(filters.TEXT & ~filters.COMMAND, pnl)]
    states[FINAL_MOVE] = [MessageHandler(filters.TEXT & ~filters.COMMAND, final_move)]
    states[FINAL_RR] = [MessageHandler(filters.TEXT & ~filters.COMMAND, final_rr)]
    states[EXIT_REASON] = [MessageHandler(filters.TEXT & ~filters.COMMAND, exit_reason)]
    states[LESSONS] = [
        MessageHandler(filters.PHOTO, lesson_or_photo),
        MessageHandler(filters.TEXT & ~filters.COMMAND, lesson_or_photo)
    ]

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_trade),
            MessageHandler(filters.Regex("^📝 معامله جدید$"), new_trade)
        ],
        states=states,
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("checklist", checklist_report))
    app.add_handler(conv)

    port = int(os.getenv("PORT", "10000"))
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if external_url:
        webhook_path = "telegram"
        webhook_url = f"{external_url}/{webhook_path}"
        print(f"Starting Telegram webhook on 0.0.0.0:{port} -> {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        # Local fallback: polling when not running on Render.
        print("Starting Telegram polling (local mode)")
        app.run_polling()

if __name__ == "__main__":
    main()
