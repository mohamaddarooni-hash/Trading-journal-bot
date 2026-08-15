
import os
import sqlite3
from datetime import datetime, timedelta
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ============================================================
# Trading Journal — Pro v2
# Built from the user's original bot.py.
# ============================================================

PRO_VERSION = "2026-08-15-pro-v2"

TOKEN = os.getenv("BOT_TOKEN")
DB = os.getenv("DB_PATH", "journal.db")

# Conversation states
SYMBOL, OTHER_SYMBOL, SIDE, ENTRY, SL, TP, LOT = range(7)
CHECKLIST_START = 7
RESULT = CHECKLIST_START + 18
EXIT_PRICE = RESULT + 1
FINAL_MOVE = EXIT_PRICE + 1
SCREENSHOT = FINAL_MOVE + 1
BALANCE_SETUP = SCREENSHOT + 1

# The user's original 6-stage checklist is preserved.
CHECKLIST = [
    ("1️⃣ LIQUIDITY (4H)", [
        "ناحیه نقدینگی در 4 ساعت گذشته مشخص شد.",
        "بر اساس فلو، ناحیه مورد نظر شناسایی شد."
    ]),
    ("2️⃣ ORDER BLOCK (1H)", [
        "در محدوده نقدینگی، اردربلاک پیدا شد.",
        "بازگشت قیمت با سناریوی حرکتی/جهتی هماهنگ است."
    ]),
    ("3️⃣ MARKET TURNING POINT (15M)", [
        "تشکیل بازار (CRT) تأیید شد.",
        "تشکیل CHoCH تأیید شد.",
        "تشکیل MSS تأیید شد.",
        "تشکیل EBP تأیید شد.",
        "نقطه چرخش معتبر است."
    ]),
    ("4️⃣ FVG / IFVG", [
        "گپ ارزش منصفانه (FVG) در جهت معامله وجود دارد.",
        "با IFVG معتبر، تأیید شده است.",
        "قیمت به ساختار بازار احترام گذاشته است."
    ]),
    ("5️⃣ VOLUME PROFILE / POC", [
        "POC با حجم زیاد مشخص است.",
        "POC در 4 ساعت گذشته مشخص شده است.",
        "قیمت نسبت به POC واکنش نشان داده است.",
        "تأیید سناریو توسط POC صورت گرفته است."
    ]),
    ("6️⃣ ENTRY (1M)", [
        "قیمت به محدوده اردربلاک تایم 1M برگشته است.",
        "اردربلاک با POC منطقی/هم‌راستا است.",
        "ریسک به ریوارد مناسب است.",
        "ورود پس از دریافت تأیید انجام شد."
    ])
]
CHECK_ITEMS = [(section, q) for section, qs in CHECKLIST for q in qs]
TOTAL_CHECKS = len(CHECK_ITEMS)


# ------------------------------------------------------------
# Database / migration
# ------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS account (
        user_id INTEGER PRIMARY KEY,
        initial_balance REAL NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        symbol TEXT,
        side TEXT,
        entry REAL,
        sl REAL,
        tp REAL,
        lot REAL,
        balance_before REAL,
        balance_after REAL,
        result TEXT,
        exit_price REAL,
        pnl REAL,
        planned_rr REAL,
        final_move REAL,
        final_rr REAL,
        missed_profit REAL,
        checklist TEXT NOT NULL,
        screenshot_file_id TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS risk_settings (
        user_id INTEGER PRIMARY KEY,
        daily_loss_limit REAL DEFAULT 20
    )
    """)

    # Compatibility with the original DB: old tables/columns are left intact.
    # A separate trades_pro table avoids destructive migration of old records.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS trades_pro (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        symbol TEXT,
        side TEXT,
        entry REAL,
        sl REAL,
        tp REAL,
        lot REAL,
        balance_before REAL,
        balance_after REAL,
        result TEXT,
        exit_price REAL,
        pnl REAL,
        planned_rr REAL,
        final_move REAL,
        final_rr REAL,
        missed_profit REAL,
        checklist TEXT NOT NULL,
        screenshot_file_id TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS migrations (
        name TEXT PRIMARY KEY,
        completed_at TEXT NOT NULL
    )
    """)

    # Preserve records from the original bot on first startup.
    # Old records are copied without inventing exit prices or missed-profit values.
    migrated = conn.execute(
        "SELECT 1 FROM migrations WHERE name='original_trades_to_pro'"
    ).fetchone()
    if not migrated:
        old_rows = conn.execute(
            """SELECT user_id, created_at, symbol, side, entry, sl, tp, lot,
                      balance_before, result, pnl, final_move, final_rr,
                      checklist, screenshot_file_id
               FROM trades ORDER BY id ASC"""
        ).fetchall()

        for row in old_rows:
            (
                user_id, created_at, symbol, side, entry, sl, tp, lot,
                balance_before, result, pnl_value, final_move, old_rr,
                checklist, screenshot_file_id
            ) = row

            before = float(balance_before) if balance_before is not None else None
            after = (before + float(pnl_value)) if before is not None and pnl_value is not None else None

            # Original checklist has the same 20 criteria.
            checklist_value = checklist or (",".join(["0"] * TOTAL_CHECKS))

            conn.execute("""
                INSERT INTO trades_pro (
                    user_id, created_at, symbol, side, entry, sl, tp, lot,
                    balance_before, balance_after, result, exit_price, pnl,
                    planned_rr, final_move, final_rr, missed_profit,
                    checklist, screenshot_file_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                user_id, created_at, symbol, side, entry, sl, tp, lot,
                before, after, result, None, pnl_value,
                None, None, old_rr, 0.0,
                checklist_value, screenshot_file_id
            ))

        conn.execute(
            "INSERT INTO migrations(name, completed_at) VALUES(?,?)",
            ("original_trades_to_pro", datetime.now().isoformat(timespec="seconds"))
        )

    conn.commit()
    return conn


def get_initial_balance(user_id):
    conn = db()
    row = conn.execute(
        "SELECT initial_balance FROM account WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return float(row[0]) if row else None


def set_initial_balance(user_id, value):
    conn = db()
    conn.execute(
        """INSERT INTO account(user_id, initial_balance, created_at)
           VALUES(?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET initial_balance=excluded.initial_balance""",
        (user_id, value, datetime.now().isoformat(timespec="seconds"))
    )
    conn.commit()
    conn.close()


def current_balance(user_id):
    initial = get_initial_balance(user_id)
    conn = db()
    row = conn.execute(
        """SELECT balance_after FROM trades_pro
           WHERE user_id=? AND balance_after IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (user_id,)
    ).fetchone()
    conn.close()
    return float(row[0]) if row else initial


# ------------------------------------------------------------
# Account-specific P/L rules supplied by the user
# ------------------------------------------------------------

def pnl_rate(symbol):
    """
    Dollars per one price unit at 0.01 lot.
    User's account rules:
      XAUUSD: 0.01 lot -> $1 per $1 move
      NAS100: 0.01 lot -> $1 per 100 index points
      US30:   0.01 lot -> $1 per 100 index points
    """
    return {
        "XAUUSD": 1.0,
        "NAS100": 0.01,
        "US30": 0.01,
    }.get(symbol.upper(), 1.0)


def money_from_move(symbol, lot, price_move):
    return price_move * (lot / 0.01) * pnl_rate(symbol)


def actual_pnl(d):
    if d["result"] == "BE":
        return 0.0
    move = d["exit_price"] - d["entry"]
    if d["side"] == "SELL":
        move = -move
    return money_from_move(d["symbol"], d["lot"], move)


def planned_rr(d):
    risk = abs(d["entry"] - d["sl"])
    reward = abs(d["tp"] - d["entry"])
    return reward / risk if risk else 0.0


def final_rr(d):
    risk = abs(d["entry"] - d["sl"])
    move = d["exit_price"] - d["entry"]
    if d["side"] == "SELL":
        move = -move
    return move / risk if risk else 0.0


def missed_profit(d):
    extra = d["final_move"] - d["exit_price"]
    if d["side"] == "SELL":
        extra = d["exit_price"] - d["final_move"]
    extra = max(0.0, extra)
    return money_from_move(d["symbol"], d["lot"], extra)


def pct(n, d):
    return 0 if not d else n / d * 100


# ------------------------------------------------------------
# UI
# ------------------------------------------------------------

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📝 معامله جدید", "📊 داشبورد"],
            ["📅 امروز", "📈 هفته", "🗓 ماه"],
            ["🏆 رکوردها", "🔎 نمادها"],
            ["📋 چک‌لیست", "⚙️ تنظیمات"]
        ],
        resize_keyboard=True
    )


async def start(update, context):
    user_id = update.effective_user.id
    balance = current_balance(user_id)

    if balance is None:
        await update.message.reply_text(
            "👋 سلام! آماده‌ای ژورنالت رو حرفه‌ای‌تر کنیم؟\n\n"
            "قبل از اولین معامله فقط یک‌بار سرمایه اولیه رو ثبت کن 💰",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["awaiting_initial_balance"] = True
        return

    await update.message.reply_text(
        f"📒 <b>Trading Journal Pro</b>\n\n"
        f"💰 سرمایه فعلی: <b>${balance:.2f}</b>\n"
        "از منوی پایین هر بخشی رو که خواستی انتخاب کن 👇",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


async def setup_balance(update, context):
    try:
        value = float(update.message.text.replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("فقط یک عدد مثبت وارد کن؛ مثلاً 600")
        return

    set_initial_balance(update.effective_user.id, value)
    context.user_data.pop("awaiting_initial_balance", None)
    await update.message.reply_text(
        f"✅ سرمایه اولیه <b>${value:.2f}</b> ثبت شد.\n\n"
        "از این به بعد بعد از هر معامله، سرمایه فعلی خودکار محاسبه می‌شه 🚀",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


async def help_cmd(update, context):
    await update.message.reply_text(
        "🧭 <b>راهنما</b>\n\n"
        "/new — معامله جدید\n"
        "/dashboard — داشبورد\n"
        "/today — گزارش امروز\n"
        "/week — گزارش ۷ روز اخیر\n"
        "/month — گزارش ۳۰ روز اخیر\n"
        "/checklist — تحلیل چک‌لیست\n"
        "/records — رکوردها\n"
        "/symbols — عملکرد نمادها\n"
        "/cancel — لغو معامله",
        parse_mode="HTML"
    )


# ------------------------------------------------------------
# New trade flow
# ------------------------------------------------------------

async def new_trade(update, context):
    if get_initial_balance(update.effective_user.id) is None:
        await update.message.reply_text(
            "💰 اول سرمایه اولیه رو ثبت کن؛ فقط یک‌بار لازم است.\n"
            "مثلاً: 600"
        )
        context.user_data["awaiting_initial_balance"] = True
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["checks"] = [False] * TOTAL_CHECKS
    context.user_data["check_stage"] = 0

    await update.message.reply_text(
        "📝 <b>معامله جدید</b>\n\n"
        "نماد رو انتخاب کن:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [["🟡 XAUUSD", "🔵 NAS100"], ["🟣 US30", "➕ نماد دیگر"]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    return SYMBOL


async def symbol(update, context):
    t = update.message.text.strip().upper()
    mapping = {
        "🟡 XAUUSD": "XAUUSD",
        "🔵 NAS100": "NAS100",
        "🟣 US30": "US30"
    }

    if t in mapping:
        context.user_data["symbol"] = mapping[t]
    elif "نماد دیگر" in t:
        await update.message.reply_text("✍️ نماد موردنظر رو تایپ کن:")
        return OTHER_SYMBOL
    else:
        await update.message.reply_text("یکی از گزینه‌ها رو انتخاب کن.")
        return SYMBOL

    await update.message.reply_text(
        "📍 جهت معامله رو انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(
            [["🟢 BUY", "🔴 SELL"]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    return SIDE


async def other_symbol(update, context):
    value = update.message.text.strip().upper()
    if not value:
        await update.message.reply_text("نماد خالی نباشه.")
        return OTHER_SYMBOL
    context.user_data["symbol"] = value
    await update.message.reply_text(
        "📍 جهت معامله رو انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(
            [["🟢 BUY", "🔴 SELL"]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
    return SIDE


async def side(update, context):
    t = update.message.text.strip().upper()
    if "BUY" in t:
        context.user_data["side"] = "BUY"
    elif "SELL" in t:
        context.user_data["side"] = "SELL"
    else:
        await update.message.reply_text("BUY یا SELL رو انتخاب کن.")
        return SIDE

    await update.message.reply_text("🎯 نقطه ورود رو وارد کن:")
    return ENTRY


async def number_field(update, context, key, prompt, next_state):
    try:
        value = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("لطفاً فقط عدد وارد کن؛ مثلاً 3345.20")
        return next_state - 1

    context.user_data[key] = value
    await update.message.reply_text(prompt)
    return next_state


async def entry(update, context):
    return await number_field(update, context, "entry", "🛑 Stop Loss رو وارد کن:", SL)


async def sl(update, context):
    return await number_field(update, context, "sl", "🎯 Take Profit رو وارد کن:", TP)


async def tp(update, context):
    return await number_field(update, context, "tp", "📦 حجم معامله رو وارد کن؛ مثلاً 0.01:", LOT)


async def lot(update, context):
    try:
        value = float(update.message.text.replace(",", "."))
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("حجم باید عدد مثبت باشه؛ مثلاً 0.01")
        return LOT

    context.user_data["lot"] = value
    rr = planned_rr(context.user_data)

    await update.message.reply_text(
        f"📐 R:R برنامه‌ریزی‌شده: <b>1:{rr:.2f}</b>\n\n"
        "حالا چک‌لیست ۶ مرحله‌ای رو با تیک ثبت کنیم ☑️",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    return await show_check_stage(update, context)


# ------------------------------------------------------------
# Six-stage checklist
# ------------------------------------------------------------

def stage_bounds(stage):
    start = sum(len(CHECKLIST[i][1]) for i in range(stage))
    end = start + len(CHECKLIST[stage][1])
    return start, end


async def show_check_stage(update, context):
    stage = context.user_data.get("check_stage", 0)
    start, end = stage_bounds(stage)

    buttons = []
    for i in range(start, end):
        mark = "☑️" if context.user_data["checks"][i] else "⬜"
        buttons.append([
            InlineKeyboardButton(
                f"{mark} {CHECKLIST[stage][1][i-start]}",
                callback_data=f"check:toggle:{i}"
            )
        ])

    nav = []
    if stage > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data="check:prev"))
    if stage < len(CHECKLIST) - 1:
        nav.append(InlineKeyboardButton("➡️ مرحله بعد", callback_data="check:next"))
    else:
        nav.append(InlineKeyboardButton("✅ پایان", callback_data="check:done"))
    buttons.append(nav)

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"<b>{CHECKLIST[stage][0]}</b>\n"
            f"مرحله {stage + 1} از 6\n\n"
            "هر موردی که برقرار است تیک بزن 👇"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CHECKLIST_START + stage


async def checklist_callback(update, context):
    q = update.callback_query
    await q.answer()

    data = q.data
    stage = context.user_data.get("check_stage", 0)

    if data.startswith("check:toggle:"):
        idx = int(data.rsplit(":", 1)[1])
        context.user_data["checks"][idx] = not context.user_data["checks"][idx]

    elif data == "check:prev":
        context.user_data["check_stage"] = max(0, stage - 1)

    elif data == "check:next":
        context.user_data["check_stage"] = min(5, stage + 1)

    elif data == "check:done":
        score = sum(context.user_data["checks"])
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(
            f"✅ <b>چک‌لیست کامل شد</b>\n\n"
            f"☑️ امتیاز: <b>{score}/{TOTAL_CHECKS}</b> "
            f"({pct(score, TOTAL_CHECKS):.1f}%)\n\n"
            "حالا نتیجه معامله رو انتخاب کن:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🟢 WIN", callback_data="result:WIN"),
                InlineKeyboardButton("🔴 LOSS", callback_data="result:LOSS"),
                InlineKeyboardButton("➖ BE", callback_data="result:BE")
            ]])
        )
        return RESULT

    # Re-render current stage.
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    return await show_check_stage(update, context)


async def checklist_text_fallback(update, context):
    await update.message.reply_text(
        "☑️ برای چک‌لیست از دکمه‌های روی پیام استفاده کن."
    )
    return CHECKLIST_START + context.user_data.get("check_stage", 0)


# ------------------------------------------------------------
# Result -> exit -> final move -> screenshot
# ------------------------------------------------------------

async def result_callback(update, context):
    q = update.callback_query
    await q.answer()
    result = q.data.split(":")[1]
    context.user_data["result"] = result

    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(
        f"🎯 نتیجه: <b>{result}</b>\n\n"
        "حالا <b>نقطه خروج واقعی</b> رو وارد کن.\n"
        "ممکنه با TP اولیه فرق کرده باشه.",
        parse_mode="HTML"
    )
    return EXIT_PRICE


async def exit_price(update, context):
    try:
        value = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("فقط قیمت خروج واقعی رو وارد کن.")
        return EXIT_PRICE

    context.user_data["exit_price"] = value
    d = context.user_data

    pnl = actual_pnl(d)
    rr = final_rr(d)

    await update.message.reply_text(
        f"📌 خروج واقعی: <b>{value}</b>\n"
        f"💵 P/L: <b>{pnl:+.2f}$</b>\n"
        f"📐 R:R واقعی: <b>1:{rr:.2f}</b>\n\n"
        "🚀 حالا گام نهایی حرکت قیمت رو وارد کن؛ "
        "یعنی بیشترین قیمت بعد از خروج برای BUY یا کمترین قیمت بعد از خروج برای SELL.",
        parse_mode="HTML"
    )
    return FINAL_MOVE


async def final_move(update, context):
    try:
        value = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("فقط قیمت Final Move رو وارد کن.")
        return FINAL_MOVE

    context.user_data["final_move"] = value
    missed = missed_profit(context.user_data)

    await update.message.reply_text(
        f"💸 سود اضافه‌ای که اگر می‌موندی می‌توانستی بگیری: "
        f"<b>${missed:.2f}</b>\n\n"
        "📸 اسکرین‌شات معامله رو بفرست.\n"
        "اگر عکس نداری، «بدون عکس» بنویس.",
        parse_mode="HTML"
    )
    return SCREENSHOT


async def screenshot(update, context):
    if update.message.photo:
        context.user_data["screenshot_file_id"] = update.message.photo[-1].file_id
        await save_trade(update, context)
        return ConversationHandler.END

    if update.message.text and update.message.text.strip() in ("بدون عکس", "ندارم"):
        context.user_data["screenshot_file_id"] = None
        await save_trade(update, context)
        return ConversationHandler.END

    await update.message.reply_text(
        "📸 عکس معامله رو بفرست یا «بدون عکس» بنویس."
    )
    return SCREENSHOT


# ------------------------------------------------------------
# Save / reports
# ------------------------------------------------------------

def save_trade_row(d, user_id):
    pnl = actual_pnl(d)
    rr = final_rr(d)
    missed = missed_profit(d)
    before = current_balance(user_id)
    after = before + pnl

    conn = db()
    cur = conn.execute("""
        INSERT INTO trades_pro (
            user_id, created_at, symbol, side, entry, sl, tp, lot,
            balance_before, balance_after, result, exit_price, pnl,
            planned_rr, final_move, final_rr, missed_profit,
            checklist, screenshot_file_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        user_id,
        datetime.now().isoformat(timespec="seconds"),
        d["symbol"], d["side"], d["entry"], d["sl"], d["tp"], d["lot"],
        before, after, d["result"], d["exit_price"], pnl,
        planned_rr(d), d["final_move"], rr, missed,
        ",".join("1" if x else "0" for x in d["checks"]),
        d.get("screenshot_file_id")
    ))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()

    return trade_id, pnl, rr, missed, before, after


async def save_trade(update, context):
    d = context.user_data
    user_id = update.effective_user.id
    trade_id, pnl, rr, missed, before, after = save_trade_row(d, user_id)

    score = sum(d["checks"])
    quality = pct(score, TOTAL_CHECKS)

    caption = (
        f"📌 <b>Trade #{trade_id}</b>\n"
        f"{d['symbol']} · {d['side']}\n\n"
        f"🎯 Entry: {d['entry']}\n"
        f"🚪 Exit: {d['exit_price']}\n"
        f"🛑 SL: {d['sl']}\n"
        f"🎯 TP: {d['tp']}\n"
        f"📦 Lot: {d['lot']}\n\n"
        f"💰 P/L: <b>{pnl:+.2f}$</b>\n"
        f"📐 R:R: <b>1:{rr:.2f}</b>\n"
        f"☑️ Checklist: <b>{score}/{TOTAL_CHECKS}</b>\n"
        f"⭐ Quality: <b>{quality:.0f}/100</b>\n"
        f"💸 Missed Profit: <b>${missed:.2f}</b>\n"
        f"🏦 Balance: <b>${after:.2f}</b>"
    )

    if d.get("screenshot_file_id"):
        await update.message.reply_photo(
            photo=d["screenshot_file_id"],
            caption=caption,
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(caption, parse_mode="HTML")

    context.user_data.clear()
    await update.message.reply_text(
        "✅ ثبت شد. آماده معامله بعدی هستیم 😎",
        reply_markup=main_keyboard()
    )


def fetch_rows(user_id, days=None):
    conn = db()
    if days is None:
        rows = conn.execute(
            "SELECT * FROM trades_pro WHERE user_id=? ORDER BY id ASC",
            (user_id,)
        ).fetchall()
    else:
        since = datetime.now() - timedelta(days=days)
        rows = conn.execute(
            """SELECT * FROM trades_pro
               WHERE user_id=? AND created_at>=?
               ORDER BY id ASC""",
            (user_id, since.isoformat(timespec="seconds"))
        ).fetchall()
    conn.close()
    return rows


def stats(rows):
    total = len(rows)
    wins = sum(str(r[10]).upper() == "WIN" for r in rows)
    losses = sum(str(r[10]).upper() == "LOSS" for r in rows)
    be = sum(str(r[10]).upper() == "BE" for r in rows)
    pnl = sum(float(r[13] or 0) for r in rows)
    profit = sum(float(r[13] or 0) for r in rows if float(r[13] or 0) > 0)
    loss = sum(float(r[13] or 0) for r in rows if float(r[13] or 0) < 0)
    pf = profit / abs(loss) if loss else 0
    avg_rr = sum(float(r[16] or 0) for r in rows) / total if total else 0
    return total, wins, losses, be, pnl, profit, loss, pf, avg_rr


def max_drawdown(rows, initial):
    if initial is None:
        return 0.0

    peak = float(initial)
    max_dd = 0.0

    for r in rows:
        bal = float(r[10])
        peak = max(peak, bal)
        if peak > 0:
            max_dd = max(max_dd, (peak - bal) / peak * 100)

    return max_dd


def report_text(rows, title, initial):
    if not rows:
        return f"📊 <b>{title}</b>\n\nهنوز معامله‌ای در این بازه ثبت نشده."

    total, wins, losses, be, pnl, profit, loss, pf, avg_rr = stats(rows)
    balance = float(rows[-1][10])
    dd = max_drawdown(rows, initial)
    missed = sum(float(r[17] or 0) for r in rows)

    return (
        f"📊 <b>{title}</b>\n\n"
        f"🔢 معاملات: {total}\n"
        f"🟢 WIN: {wins}   🔴 LOSS: {losses}   ➖ BE: {be}\n"
        f"🎯 Win Rate: {pct(wins, total):.1f}%\n"
        f"💰 Net P/L: <b>{pnl:+.2f}$</b>\n"
        f"📈 Profit: {profit:+.2f}$\n"
        f"📉 Loss: {loss:+.2f}$\n"
        f"⚖️ Profit Factor: {pf:.2f}\n"
        f"📐 Average R: 1:{avg_rr:.2f}\n"
        f"📉 Max Drawdown: {dd:.2f}%\n"
        f"🏦 سرمایه فعلی: <b>${balance:.2f}</b>\n"
        f"💸 سود از دست‌رفته: <b>${missed:.2f}</b>"
    )


async def dashboard(update, context):
    user_id = update.effective_user.id
    rows = fetch_rows(user_id)
    await update.message.reply_text(
        report_text(rows, "داشبورد حساب", get_initial_balance(user_id)),
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


async def period_report(update, context, days, title):
    rows = fetch_rows(update.effective_user.id, days)
    await update.message.reply_text(
        report_text(rows, title, get_initial_balance(update.effective_user.id)),
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


async def today(update, context):
    await period_report(update, context, 1, "گزارش امروز")


async def week(update, context):
    await period_report(update, context, 7, "گزارش هفتگی")


async def month(update, context):
    await period_report(update, context, 30, "گزارش ماهانه")


async def records(update, context):
    rows = fetch_rows(update.effective_user.id)

    if not rows:
        await update.message.reply_text("هنوز رکوردی برای نمایش نداریم.")
        return

    best = max(rows, key=lambda r: float(r[13] or 0))
    worst = min(rows, key=lambda r: float(r[13] or 0))

    best_streak = 0
    current = 0
    for r in rows:
        if str(r[11]).upper() == "WIN":
            current += 1
            best_streak = max(best_streak, current)
        else:
            current = 0

    missed = sum(float(r[17] or 0) for r in rows)

    await update.message.reply_text(
        "🏆 <b>رکوردهای شخصی</b>\n\n"
        f"🥇 بهترین معامله: {float(best[13]):+.2f}$ — {best[3]}\n"
        f"💔 بدترین معامله: {float(worst[13]):+.2f}$ — {worst[3]}\n"
        f"🔥 بیشترین برد متوالی: {best_streak}\n"
        f"💸 مجموع سود از دست‌رفته: ${missed:.2f}",
        parse_mode="HTML"
    )


async def symbols(update, context):
    rows = fetch_rows(update.effective_user.id)

    if not rows:
        await update.message.reply_text("هنوز معامله‌ای برای تحلیل نماد نداریم.")
        return

    lines = ["🔎 <b>عملکرد نمادها</b>\n"]

    for symbol in sorted(set(r[3] for r in rows)):
        rs = [r for r in rows if r[3] == symbol]
        t, w, l, b, pnl, *_ = stats(rs)
        lines.append(
            f"<b>{symbol}</b> — {t} معامله | "
            f"Win Rate {pct(w,t):.1f}% | P/L {pnl:+.2f}$"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def checklist_report(update, context):
    rows = fetch_rows(update.effective_user.id, 30)

    if not rows:
        await update.message.reply_text(
            "📋 در ۳۰ روز اخیر معامله‌ای برای تحلیل چک‌لیست نداریم."
        )
        return

    lines = ["📋 <b>تحلیل چک‌لیست — ۳۰ روز اخیر</b>\n"]

    for idx, (section, questions) in enumerate(CHECKLIST):
        start, end = stage_bounds(idx)
        values = []
        for qidx in range(start, end):
            yes = sum(
                1 for r in rows
                if r[18].split(",")[qidx] == "1"
            )
            values.append(yes)

        total_possible = len(rows) * len(questions)
        lines.append(
            f"{section}: {pct(sum(values), total_possible):.1f}%"
        )

    # Weakest individual rules
    item_rates = []
    for idx, (section, question) in enumerate(CHECK_ITEMS):
        yes = sum(
            1 for r in rows
            if r[18].split(",")[idx] == "1"
        )
        item_rates.append((pct(yes, len(rows)), question))

    lines.append("\n⚠️ <b>ضعیف‌ترین موارد:</b>")
    for rate, question in sorted(item_rates)[:5]:
        lines.append(f"• {rate:.0f}% — {question}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML"
    )


async def settings(update, context):
    await update.message.reply_text(
        "⚙️ <b>تنظیمات</b>\n\n"
        f"نسخه: <code>{PRO_VERSION}</code>\n"
        f"💰 سرمایه اولیه: ${get_initial_balance(update.effective_user.id) or 0:.2f}\n\n"
        "ضرایب P/L حساب:\n"
        "🟡 XAUUSD — 0.01 lot / هر $1 حرکت = $1\n"
        "🔵 NAS100 — 0.01 lot / هر 100 واحد = $1\n"
        "🟣 US30 — 0.01 lot / هر 100 واحد = $1",
        parse_mode="HTML"
    )


async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ ثبت معامله لغو شد.",
        reply_markup=main_keyboard()
    )
    return ConversationHandler.END


# ------------------------------------------------------------
# App
# ------------------------------------------------------------

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    db()

    app = Application.builder().token(TOKEN).build()

    states = {
        SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, symbol)],
        OTHER_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, other_symbol)],
        SIDE: [MessageHandler(filters.TEXT & ~filters.COMMAND, side)],
        ENTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, entry)],
        SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, sl)],
        TP: [MessageHandler(filters.TEXT & ~filters.COMMAND, tp)],
        LOT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lot)],
    }

    for i in range(6):
        states[CHECKLIST_START + i] = [
            CallbackQueryHandler(
                checklist_callback,
                pattern=r"^check:(toggle:\d+|prev|next|done)$"
            ),
            MessageHandler(filters.TEXT & ~filters.COMMAND, checklist_text_fallback)
        ]

    states[RESULT] = [
        CallbackQueryHandler(result_callback, pattern=r"^result:(WIN|LOSS|BE)$")
    ]
    states[EXIT_PRICE] = [
        MessageHandler(filters.TEXT & ~filters.COMMAND, exit_price)
    ]
    states[FINAL_MOVE] = [
        MessageHandler(filters.TEXT & ~filters.COMMAND, final_move)
    ]
    states[SCREENSHOT] = [
        MessageHandler(filters.PHOTO, screenshot),
        MessageHandler(filters.TEXT & ~filters.COMMAND, screenshot)
    ]

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_trade),
            MessageHandler(filters.Regex("^📝 معامله جدید$"), new_trade)
        ],
        states=states,
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("records", records))
    app.add_handler(CommandHandler("symbols", symbols))
    app.add_handler(CommandHandler("checklist", checklist_report))
    app.add_handler(conv)

    app.add_handler(MessageHandler(filters.Regex("^📊 داشبورد$"), dashboard))
    app.add_handler(MessageHandler(filters.Regex("^📅 امروز$"), today))
    app.add_handler(MessageHandler(filters.Regex("^📈 هفته$"), week))
    app.add_handler(MessageHandler(filters.Regex("^🗓 ماه$"), month))
    app.add_handler(MessageHandler(filters.Regex("^🏆 رکوردها$"), records))
    app.add_handler(MessageHandler(filters.Regex("^🔎 نمادها$"), symbols))
    app.add_handler(MessageHandler(filters.Regex("^📋 چک‌لیست$"), checklist_report))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ تنظیمات$"), settings))

    # Initial balance can be entered outside the conversation.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            setup_balance
        )
    )

    port = int(os.getenv("PORT", "10000"))
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

    if external_url:
        webhook_path = "telegram"
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=f"{external_url}/{webhook_path}",
            drop_pending_updates=True
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
