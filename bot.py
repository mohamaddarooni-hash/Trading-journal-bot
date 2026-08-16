
import os
import sqlite3
from datetime import datetime, timedelta, date
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
DB = os.getenv("DB_PATH", "journal.db")
VERSION = "2026-08-16-pro-v3"

# -------------------- Conversation states --------------------
BALANCE_SETUP, SYMBOL, OTHER_SYMBOL, SIDE, ENTRY, SL, TP, LOT = range(8)
CHECKLIST_START = 8
RESULT = 9
EXIT_PRICE = 10
FINAL_MOVE = 11
SCREENSHOT = 12

# -------------------- Exact 6-stage checklist --------------------
CHECKLIST = [
    ("1️⃣ نقدینگی — 4H", [
        "شناسایی نواحی مهم نقدینگی در تایم‌فریم ۴ ساعته",
        "همپوشانی سطوح مهم فیبوناچی با نواحی مهم در گام حرکتی",
    ]),
    ("2️⃣ محدوده نقدینگی", [
        "شناسایی اردربلاک معتبر در محدوده نقدینگی",
        "شناسایی FVG یا سطوح مهم در محدوده نقدینگی",
        "ریجکشن اولیه قیمت از محدوده‌های مورد انتظار",
    ]),
    ("3️⃣ ساختار بازار", [
        "CRT + TBS",
        "CHoCH + BOS",
        "MSS + CISD",
        "EBP",
    ]),
    ("4️⃣ FVG / IFVG", [
        "شناسایی FVG",
        "شناسایی IFVG",
        "قیمت به نواحی واکنش معتبر داشته است",
    ]),
    ("5️⃣ POC", [
        "مشخص نمودن POC در گام حرکتی ۴ ساعته",
        "مشخص نمودن POC در گام اصلاحی شکل گرفته",
        "سناریو انطباق POCها با OB یک‌ساعته تأیید شد",
    ]),
    ("6️⃣ ورود — 1M", [
        "قیمت در تایم‌فریم ۱ دقیقه به محدوده مناسب ورود رسیده است",
        "مقایسه ساختار تایم‌های ۱، ۵ و ۱۵ دقیقه در جهت سناریوی ورود بود",
        "ریسک به ریوارد در نقطه ورود کنترل شد",
        "ورود بعد از ریجکت مجدد قیمت در تایم‌فریم ۱ دقیقه انجام شد",
    ]),
]
CHECK_ITEMS = [(s, q) for s, qs in CHECKLIST for q in qs]
TOTAL_CHECKS = len(CHECK_ITEMS)

# -------------------- DB --------------------
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
    CREATE TABLE IF NOT EXISTS capital_adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        adjustment_type TEXT NOT NULL,
        amount REAL NOT NULL,
        note TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        symbol TEXT, side TEXT, entry REAL, sl REAL, tp REAL, lot REAL,
        balance_before REAL,
        reason TEXT, emotion TEXT,
        checklist TEXT NOT NULL,
        checklist_notes TEXT,
        result TEXT, pnl REAL, final_move TEXT, final_rr REAL,
        exit_reason TEXT, lessons TEXT,
        screenshot_file_id TEXT
    )
    """)
    # Non-destructive migration for the existing journal.db.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    additions = {
        "exit_price": "REAL",
        "balance_after": "REAL",
        "day_start_balance": "REAL",
        "drawdown_pct": "REAL",
        "missed_profit": "REAL",
        "planned_rr": "REAL",
    }
    for name, typ in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {typ}")
    conn.commit()
    return conn

def pct(n, d):
    return 0.0 if not d else n / d * 100.0

def initial_balance(user_id):
    conn = db()
    row = conn.execute(
        "SELECT initial_balance FROM account WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return float(row[0]) if row else None

def set_initial_balance(user_id, value):
    conn = db()
    conn.execute("""
        INSERT INTO account(user_id, initial_balance, created_at)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET initial_balance=excluded.initial_balance
    """, (user_id, value, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

def current_balance(user_id):
    start = initial_balance(user_id)
    if start is None:
        return None
    conn = db()
    trade_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    adjustments = conn.execute(
        """SELECT COALESCE(SUM(
               CASE WHEN adjustment_type='DEPOSIT' THEN amount
                    WHEN adjustment_type='WITHDRAW' THEN -amount
                    ELSE 0 END
             ),0)
           FROM capital_adjustments WHERE user_id=?""", (user_id,)
    ).fetchone()[0]
    conn.close()
    return float(start) + float(trade_pnl or 0) + float(adjustments or 0)

def start_of_day_balance(user_id):
    conn = db()
    today = date.today().isoformat()
    row = conn.execute("""
        SELECT day_start_balance FROM trades
        WHERE user_id=? AND substr(created_at,1,10)=?
          AND day_start_balance IS NOT NULL
        ORDER BY id ASC LIMIT 1
    """, (user_id, today)).fetchone()
    conn.close()
    if row:
        return float(row[0])
    return current_balance(user_id)

# -------------------- User's exact contract rules --------------------
def money_from_move(symbol, lot, price_move):
    rate = {
        "XAUUSD": 1.0,   # 0.01 lot -> $1 per $1 move
        "NAS100": 0.01,  # 0.01 lot -> $1 per 100 points
        "US30": 0.01,    # 0.01 lot -> $1 per 100 points
    }.get(symbol.upper(), 1.0)
    return price_move * (lot / 0.01) * rate

def signed_move(d, price):
    move = price - d["entry"]
    return -move if d["side"] == "SELL" else move

def planned_rr(d):
    risk = abs(d["entry"] - d["sl"])
    reward = abs(d["tp"] - d["entry"])
    return reward / risk if risk else 0.0

def actual_pnl(d):
    move = signed_move(d, d["exit_price"])
    if d["result"] == "BE":
        return 0.0
    return money_from_move(d["symbol"], d["lot"], move)

def final_rr(d):
    risk = abs(d["entry"] - d["sl"])
    move = signed_move(d, d["exit_price"])
    return move / risk if risk else 0.0

def missed_profit(d):
    # Profit available after the actual exit, in the favorable direction.
    extra = signed_move(d, d["final_move"]) - signed_move(d, d["exit_price"])
    extra = max(0.0, extra)
    return money_from_move(d["symbol"], d["lot"], extra)

# -------------------- UI --------------------
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📝 معامله جدید", "📊 داشبورد"],
            ["📅 امروز", "📈 هفته", "🗓 ماه"],
            ["📋 تحلیل چک‌لیست", "🏆 رکوردها"],
            ["💰 مدیریت سرمایه", "⚙️ تنظیمات"],
            ["🔎 نمادها"],
        ],
        resize_keyboard=True
    )

def volume_keyboard():
    vals = [f"{i/100:.2f}" for i in range(1, 11)]
    rows = [vals[i:i+5] for i in range(0, 10, 5)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

def symbol_keyboard():
    return ReplyKeyboardMarkup(
        [["🟡 XAUUSD", "🔵 NAS100"], ["🟣 US30", "➕ نماد دیگر"]],
        resize_keyboard=True, one_time_keyboard=True
    )

def side_keyboard():
    return ReplyKeyboardMarkup(
        [["🟢 BUY", "🔴 SELL"]],
        resize_keyboard=True, one_time_keyboard=True
    )

async def start(update, context):
    uid = update.effective_user.id
    bal = current_balance(uid)
    if bal is None:
        context.user_data["awaiting_initial_balance"] = True
        await update.message.reply_text(
            "👋 خوش اومدی به ژورنال حرفه‌ای.\n\n"
            "💰 اول سرمایه/مارجین کلی حسابت رو یک‌بار وارد کن؛ "
            "بعد از هر معامله سرمایه فعلی خودکار محاسبه می‌شه."
        )
        return
    await update.message.reply_text(
        f"📒 <b>Trading Journal Pro</b>\n"
        f"💰 سرمایه فعلی: <b>${bal:.2f}</b>",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def setup_balance(update, context):
    if not context.user_data.get("awaiting_initial_balance"):
        return
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
        f"✅ سرمایه اولیه <b>${value:.2f}</b> ثبت شد.\n"
        "از این به بعد سرمایه فعلی بعد از هر معامله خودکار محاسبه می‌شه.",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def new_trade(update, context):
    if initial_balance(update.effective_user.id) is None:
        context.user_data["awaiting_initial_balance"] = True
        await update.message.reply_text("💰 اول سرمایه کلی رو وارد کن؛ مثلاً 600")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["checks"] = []
    context.user_data["check_idx"] = 0
    await update.message.reply_text(
        "📝 <b>معامله جدید</b>\n\nنماد رو انتخاب کن:",
        parse_mode="HTML", reply_markup=symbol_keyboard()
    )
    return SYMBOL

async def symbol(update, context):
    t = update.message.text.strip()
    mapping = {"🟡 XAUUSD":"XAUUSD","🔵 NAS100":"NAS100","🟣 US30":"US30"}
    if t in mapping:
        context.user_data["symbol"] = mapping[t]
    elif "نماد دیگر" in t:
        await update.message.reply_text("✍️ نماد موردنظر رو تایپ کن:")
        return OTHER_SYMBOL
    else:
        await update.message.reply_text("یکی از گزینه‌ها رو انتخاب کن.")
        return SYMBOL
    await update.message.reply_text("📍 جهت معامله رو انتخاب کن:", reply_markup=side_keyboard())
    return SIDE

async def other_symbol(update, context):
    s = update.message.text.strip().upper()
    if not s:
        await update.message.reply_text("نماد خالی نباشه.")
        return OTHER_SYMBOL
    context.user_data["symbol"] = s
    await update.message.reply_text("📍 جهت معامله رو انتخاب کن:", reply_markup=side_keyboard())
    return SIDE

async def side(update, context):
    t = update.message.text.upper()
    if "BUY" in t:
        context.user_data["side"] = "BUY"
    elif "SELL" in t:
        context.user_data["side"] = "SELL"
    else:
        await update.message.reply_text("BUY یا SELL رو انتخاب کن.")
        return SIDE
    await update.message.reply_text("🎯 نقطه ورود رو وارد کن:")
    return ENTRY

async def number_field(update, context, key, prompt, state):
    try:
        v = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("لطفاً فقط عدد وارد کن.")
        return state
    context.user_data[key] = v
    await update.message.reply_text(prompt)
    return state + 1

async def entry(update, context):
    return await number_field(update, context, "entry", "🛑 Stop Loss رو وارد کن:", ENTRY)

async def sl(update, context):
    return await number_field(update, context, "sl", "🎯 Take Profit رو وارد کن:", SL)

async def tp(update, context):
    try:
        v = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("لطفاً فقط عدد وارد کن.")
        return TP
    context.user_data["tp"] = v
    await update.message.reply_text(
        "📦 حجم معامله رو انتخاب کن:",
        reply_markup=volume_keyboard()
    )
    return LOT

async def lot(update, context):
    try:
        v = float(update.message.text.replace(",", "."))
        if v < 0.01 or v > 0.10 or round(v*100) != v*100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("از لیست حجم 0.01 تا 0.10 انتخاب کن.")
        return LOT
    context.user_data["lot"] = v
    context.user_data["planned_rr"] = planned_rr(context.user_data)
    # Explicitly reset checklist cursor here so the first checklist callback
    # is always handled by the checklist state.
    context.user_data["check_idx"] = 0
    context.user_data["checks"] = []
    await update.message.reply_text(
        f"📐 R:R برنامه‌ریزی‌شده: <b>1:{context.user_data['planned_rr']:.2f}</b>\n\n"
        "حالا چک‌لیست ۶ مرحله‌ای رو بررسی کنیم ☑️",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
    )
    await ask_check(update, context)
    return CHECKLIST_START

async def ask_check(update, context):
    i = context.user_data["check_idx"]
    section, question = CHECK_ITEMS[i]
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("☑️ هست", callback_data="check_yes"),
        InlineKeyboardButton("⬜ نیست", callback_data="check_no"),
    ]])
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"<b>{section}</b>\n\n{question}",
        parse_mode="HTML", reply_markup=keyboard
    )

async def checklist_button(update, context):
    q = update.callback_query
    await q.answer()
    if q.data not in ("check_yes","check_no"):
        return CHECKLIST_START
    context.user_data["checks"].append(q.data == "check_yes")
    context.user_data["check_idx"] += 1
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if context.user_data["check_idx"] < TOTAL_CHECKS:
        await ask_check(update, context)
        return CHECKLIST_START

    score = sum(context.user_data["checks"])
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ چک‌لیست کامل شد: <b>{score}/{TOTAL_CHECKS}</b> "
             f"({pct(score,TOTAL_CHECKS):.1f}%)\n\nنتیجه معامله رو انتخاب کن:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🟢 WIN", callback_data="result_WIN"),
            InlineKeyboardButton("🔴 LOSS", callback_data="result_LOSS"),
            InlineKeyboardButton("➖ BE", callback_data="result_BE"),
        ]])
    )
    return RESULT

async def result_callback(update, context):
    q = update.callback_query
    await q.answer()
    result = q.data.split("_",1)[1]
    context.user_data["result"] = result
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text(
        "🎯 نقطه خروج واقعی رو وارد کن.\n"
        "ممکنه با TP اولیه فرق کرده باشه:"
    )
    return EXIT_PRICE

async def exit_price(update, context):
    try:
        v = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("فقط قیمت خروج واقعی رو وارد کن.")
        return EXIT_PRICE
    context.user_data["exit_price"] = v
    d = context.user_data
    pnl = actual_pnl(d)
    rr = final_rr(d)
    await update.message.reply_text(
        f"💵 P/L معامله: <b>{pnl:+.2f}$</b>\n"
        f"📐 R:R واقعی: <b>1:{rr:.2f}</b>\n\n"
        "🚀 حالا Final Move رو وارد کن؛ یعنی بیشترین حرکت بعد از خروج "
        "در جهت معامله.",
        parse_mode="HTML"
    )
    return FINAL_MOVE

async def final_move(update, context):
    try:
        v = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("فقط قیمت Final Move رو وارد کن.")
        return FINAL_MOVE
    context.user_data["final_move"] = v
    missed = missed_profit(context.user_data)
    await update.message.reply_text(
        f"💸 اگر روی معامله می‌موندی، حدود <b>${missed:.2f}</b> سود بیشتر "
        "ممکن بود بگیری.\n\n"
        "📸 اسکرین‌شات معامله رو داری؟",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [["📸 آپلود عکس", "🚫 عکس ندارم"]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )
    return SCREENSHOT

async def screenshot_choice(update, context):
    t = update.message.text.strip()
    if t == "🚫 عکس ندارم":
        context.user_data["screenshot_file_id"] = None
        await save_trade(update, context)
        return ConversationHandler.END
    if t == "📸 آپلود عکس":
        context.user_data["awaiting_photo"] = True
        await update.message.reply_text("📸 حالا عکس معامله رو ارسال کن.")
        return SCREENSHOT
    if context.user_data.get("awaiting_photo") and update.message.photo:
        context.user_data["screenshot_file_id"] = update.message.photo[-1].file_id
        await save_trade(update, context)
        return ConversationHandler.END
    await update.message.reply_text("یکی از دو گزینه رو انتخاب کن.")
    return SCREENSHOT

async def screenshot_photo(update, context):
    if not context.user_data.get("awaiting_photo"):
        await update.message.reply_text("اول گزینه «📸 آپلود عکس» رو بزن.")
        return SCREENSHOT
    context.user_data["screenshot_file_id"] = update.message.photo[-1].file_id
    await save_trade(update, context)
    return ConversationHandler.END

# -------------------- Daily loss warning --------------------
def daily_loss_pct(user_id, after_balance, day_start):
    if not day_start or day_start <= 0:
        return 0.0
    return max(0.0, (day_start - after_balance) / day_start * 100.0)

def daily_warning(p):
    if p >= 2.0:
        return (
            "🛑 <b>برای امروز کافیه.</b>\n\n"
            "به ۲٪ افت از سرمایه شروع روز رسیدی.\n"
            "چارت رو ببند و امروز دیگه معامله نکن.\n\n"
            "<b>بازار فردا هم هست؛ سرمایه و آرامش تو از یک معامله مهم‌تره.</b>\n"
            "امروز قرار نیست ضررت رو پس بگیری؛ امروز قراره جلوی بزرگ‌تر شدنش رو بگیری. 💪"
        )
    if p >= 1.5:
        return (
            "🧠 یه لحظه مکث کن...\n\n"
            "الان مهم‌تر از پیدا کردن معامله بعدی، کنترل احساساته.\n"
            "ضرر قبلی رو با معامله بعدی جبران نکن؛ معامله بعدی باید یک تصمیم جدید باشه، "
            "نه ادامه معامله قبلی.\n\nنفس عمیق... آروم باش. 🌱"
        )
    if p >= 1.0:
        return (
            "☕ رفیق، یه وقفه کوچیک بد نیست.\n\n"
            "برو یه چایی بخور، چند دقیقه از چارت فاصله بگیر و اگر هنوز سشن معاملاتی‌ات "
            "ادامه داشت، با ذهن تازه برگرد.\n"
            "<b>قرار نیست هر حرکت بازار رو معامله کنیم.</b>"
        )
    if p >= 0.5:
        return (
            f"📊 گزارش امروز\n\n"
            f"تا این لحظه <b>{p:.2f}%</b> از سرمایه شروع روزت کاهش داشته.\n"
            "حواست به کیفیت معاملات بعدی باشه. 🎯"
        )
    return None

# -------------------- Save --------------------
async def save_trade(update, context):
    d = context.user_data
    uid = update.effective_user.id
    before = current_balance(uid)
    day_start = start_of_day_balance(uid)
    pnl = actual_pnl(d)
    after = before + pnl
    dd = daily_loss_pct(uid, after, day_start)
    rr = final_rr(d)
    missed = missed_profit(d)
    score = sum(d["checks"])

    conn = db()
    cur = conn.execute("""
        INSERT INTO trades(
            user_id, created_at, symbol, side, entry, sl, tp, lot,
            balance_before, reason, emotion, checklist, checklist_notes,
            result, pnl, final_move, final_rr, exit_reason, lessons,
            screenshot_file_id, exit_price, balance_after, day_start_balance,
            drawdown_pct, missed_profit, planned_rr
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        uid, datetime.now().isoformat(timespec="seconds"),
        d["symbol"], d["side"], d["entry"], d["sl"], d["tp"], d["lot"],
        before, None, None,
        ",".join("1" if x else "0" for x in d["checks"]),
        None, d["result"], pnl, d["final_move"], rr, None, None,
        d.get("screenshot_file_id"), d["exit_price"], after, day_start,
        dd, missed, d["planned_rr"]
    ))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()

    caption = (
        f"📌 <b>معامله #{trade_id}</b>\n"
        f"{d['symbol']} · {d['side']}\n"
        f"🎯 Entry: {d['entry']}  |  🚪 Exit: {d['exit_price']}\n"
        f"🛑 SL: {d['sl']}  |  🎯 TP: {d['tp']}\n"
        f"📦 حجم: {d['lot']:.2f}\n\n"
        f"🎯 نتیجه: <b>{d['result']}</b>\n"
        f"💰 P/L: <b>{pnl:+.2f}$</b>\n"
        f"📐 R:R: <b>1:{rr:.2f}</b>\n"
        f"☑️ چک‌لیست: <b>{score}/{TOTAL_CHECKS}</b>\n"
        f"💸 سود از دست‌رفته: <b>${missed:.2f}</b>\n"
        f"📉 افت از شروع روز: <b>{dd:.2f}%</b>\n"
        f"🏦 سرمایه فعلی: <b>${after:.2f}</b>"
    )
    if d.get("screenshot_file_id"):
        await update.message.reply_photo(
            d["screenshot_file_id"], caption=caption, parse_mode="HTML"
        )
    else:
        await update.message.reply_text(caption, parse_mode="HTML")

    # Warnings are based on start-of-day balance, not account initial balance.
    warning = daily_warning(dd)
    if warning:
        await update.message.reply_text(warning, parse_mode="HTML")

    context.user_data.clear()
    await update.message.reply_text("✅ معامله ثبت شد. آماده معامله بعدی هستیم 😎",
                                    reply_markup=main_keyboard())

# -------------------- Reports --------------------
def fetch_rows(uid, days=None):
    conn = db()
    if days is None:
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY created_at ASC", (uid,)
        ).fetchall()
    else:
        since = datetime.now() - timedelta(days=days)
        rows = conn.execute(
            """SELECT * FROM trades WHERE user_id=? AND created_at>=?
               ORDER BY created_at ASC""",
            (uid, since.isoformat(timespec="seconds"))
        ).fetchall()
    conn.close()
    return rows

# Existing schema indices:
# 0 id,1 user,2 created,3 symbol,4 side,5 entry,6 sl,7 tp,8 lot,
# 9 balance_before,10 reason,11 emotion,12 checklist,13 notes,14 result,
# 15 pnl,16 final_move,17 final_rr,18 exit_reason,19 lessons,20 screenshot,
# 21 exit_price,22 balance_after,23 day_start_balance,24 drawdown,25 missed,26 planned_rr

def stats(rows):
    total = len(rows)
    wins = sum(str(r[14]).strip().upper() == "WIN" for r in rows)
    losses = sum(str(r[14]).strip().upper() == "LOSS" for r in rows)
    be = sum(str(r[14]).strip().upper() in ("BE","BREAK EVEN","BREAKEVEN") for r in rows)
    pnl = sum(float(r[15] or 0) for r in rows)
    profit = sum(float(r[15] or 0) for r in rows if float(r[15] or 0) > 0)
    loss = sum(float(r[15] or 0) for r in rows if float(r[15] or 0) < 0)
    pf = profit / abs(loss) if loss else (float("inf") if profit > 0 else 0)
    avg_rr = sum(float(r[17] or 0) for r in rows) / total if total else 0
    return total, wins, losses, be, pnl, profit, loss, pf, avg_rr

def max_drawdown(rows):
    balances = [float(r[22]) for r in rows if r[22] is not None]
    if not balances:
        # Fallback for old trades: use balance_before + pnl.
        balances = []
        for r in rows:
            if r[9] is not None:
                balances.append(float(r[9]) + float(r[15] or 0))
    if not balances:
        return 0.0
    peak = balances[0]
    mdd = 0.0
    for b in balances:
        peak = max(peak, b)
        if peak:
            mdd = max(mdd, (peak - b) / peak * 100.0)
    return mdd

def report_text(rows, title):
    if not rows:
        return f"📊 <b>{title}</b>\n\nهنوز معامله‌ای در این بازه ثبت نشده."
    total,wins,losses,be,pnl,profit,loss,pf,avg_rr = stats(rows)
    current = float(rows[-1][22]) if rows[-1][22] is not None else None
    dd = max_drawdown(rows)
    missed = sum(float(r[25] or 0) for r in rows)
    pf_text = "∞" if pf == float("inf") else f"{pf:.2f}"
    return (
        f"📊 <b>{title}</b>\n\n"
        f"🔢 معاملات: {total}\n"
        f"🟢 WIN: <b>{wins}</b>\n"
        f"🔴 LOSS: <b>{losses}</b>\n"
        f"➖ BE: {be}\n"
        f"🎯 Win Rate: <b>{pct(wins,total):.1f}%</b>\n"
        f"💰 Net P/L: <b>{pnl:+.2f}$</b>\n"
        f"📈 Profit: {profit:+.2f}$\n"
        f"📉 Loss: {loss:+.2f}$\n"
        f"⚖️ Profit Factor: <b>{pf_text}</b>\n"
        f"📐 Average R:R: <b>1:{avg_rr:.2f}</b>\n"
        f"📉 Max Drawdown: <b>{dd:.2f}%</b>\n"
        + (f"🏦 سرمایه فعلی: <b>${current:.2f}</b>\n" if current is not None else "")
        + f"💸 سود از دست‌رفته: <b>${missed:.2f}</b>"
    )

def checklist_breakdown(rows):
    wins = [r for r in rows if str(r[14]).upper() == "WIN"]
    losses = [r for r in rows if str(r[14]).upper() == "LOSS"]
    lines = ["📋 <b>تحلیل چک‌لیست — تفکیک WIN و LOSS</b>\n"]

    for stage_idx, (section, questions) in enumerate(CHECKLIST):
        lines.append(f"\n<b>{section}</b>")
        start = sum(len(CHECKLIST[i][1]) for i in range(stage_idx))
        for j, question in enumerate(questions):
            idx = start + j
            def rate(group):
                if not group:
                    return None
                yes = sum(1 for r in group if len(r[12].split(",")) > idx and r[12].split(",")[idx] == "1")
                return pct(yes, len(group))
            wr, lr = rate(wins), rate(losses)
            wtxt = "—" if wr is None else f"{wr:.0f}%"
            ltxt = "—" if lr is None else f"{lr:.0f}%"
            diff = "—" if wr is None or lr is None else f"{wr-lr:+.0f}%"
            lines.append(f"• {question}\n  🟢 WIN: {wtxt} | 🔴 LOSS: {ltxt} | Δ: {diff}")
    if not wins or not losses:
        lines.append("\nℹ️ برای مقایسه واقعی WIN و LOSS حداقل یک مورد از هرکدام لازم است.")
    else:
        # Top differences
        diffs = []
        for idx, (_, question) in enumerate(CHECK_ITEMS):
            wr = pct(sum(1 for r in wins if r[12].split(",")[idx]=="1"), len(wins))
            lr = pct(sum(1 for r in losses if r[12].split(",")[idx]=="1"), len(losses))
            diffs.append((wr-lr, question))
        lines.append("\n💡 <b>بیشترین اختلاف WIN نسبت به LOSS:</b>")
        for d,q in sorted(diffs, reverse=True)[:5]:
            lines.append(f"• {d:+.0f}% — {q}")
        lines.append("\n⚠️ <b>بیشترین ضعف در WIN نسبت به LOSS:</b>")
        for d,q in sorted(diffs)[:5]:
            lines.append(f"• {d:+.0f}% — {q}")
    return "\n".join(lines)

async def dashboard(update, context):
    await update.message.reply_text(report_text(fetch_rows(update.effective_user.id), "داشبورد"))

async def today(update, context):
    await update.message.reply_text(report_text(fetch_rows(update.effective_user.id,1), "گزارش امروز"))

async def week(update, context):
    await update.message.reply_text(report_text(fetch_rows(update.effective_user.id,7), "گزارش هفتگی"))

async def month(update, context):
    await update.message.reply_text(report_text(fetch_rows(update.effective_user.id,30), "گزارش ماهانه"))

async def checklist_report(update, context):
    rows = fetch_rows(update.effective_user.id, 30)
    await update.message.reply_text(
        checklist_breakdown(rows) if rows else "📋 هنوز معامله‌ای برای تحلیل چک‌لیست ثبت نشده.",
        parse_mode="HTML"
    )

async def records(update, context):
    rows = fetch_rows(update.effective_user.id)
    if not rows:
        await update.message.reply_text("هنوز رکوردی ثبت نشده.")
        return
    best = max(rows, key=lambda r: float(r[15] or 0))
    worst = min(rows, key=lambda r: float(r[15] or 0))
    await update.message.reply_text(
        "🏆 <b>رکوردها</b>\n\n"
        f"🥇 بهترین معامله: {float(best[15] or 0):+.2f}$ — {best[3]}\n"
        f"💔 بدترین معامله: {float(worst[15] or 0):+.2f}$ — {worst[3]}\n"
        f"📉 Max Drawdown: {max_drawdown(rows):.2f}%",
        parse_mode="HTML"
    )

async def symbols(update, context):
    rows = fetch_rows(update.effective_user.id)
    if not rows:
        await update.message.reply_text("هنوز معامله‌ای برای تحلیل نماد نداریم.")
        return
    lines = ["🔎 <b>عملکرد نمادها</b>\n"]
    for s in sorted(set(r[3] for r in rows)):
        rs = [r for r in rows if r[3] == s]
        t,w,l,b,p,*_ = stats(rs)
        lines.append(f"<b>{s}</b> — {t} معامله | Win Rate {pct(w,t):.1f}% | P/L {p:+.2f}$")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def capital_menu(update, context):
    await update.message.reply_text(
        "💰 <b>مدیریت سرمایه</b>\n\n"
        "واریز و برداشت سرمایه جدا از معاملات ثبت می‌شود و روی Win Rate و Profit Factor اثر نمی‌گذارد.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [["➕ افزایش سرمایه", "➖ کاهش سرمایه"], ["📊 سرمایه فعلی", "↩️ بازگشت"]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )

async def capital_adjustment(update, context):
    t = update.message.text.strip()
    if t == "📊 سرمایه فعلی":
        bal = current_balance(update.effective_user.id)
        await update.message.reply_text(
            f"🏦 سرمایه فعلی: <b>${bal:.2f}</b>",
            parse_mode="HTML", reply_markup=main_keyboard()
        )
        return
    if t == "↩️ بازگشت":
        await update.message.reply_text("برگشتیم به منوی اصلی 😎", reply_markup=main_keyboard())
        return
    if t == "➕ افزایش سرمایه":
        context.user_data["capital_type"] = "DEPOSIT"
        await update.message.reply_text("💵 مبلغ افزایش سرمایه رو وارد کن:")
        return
    if t == "➖ کاهش سرمایه":
        context.user_data["capital_type"] = "WITHDRAW"
        await update.message.reply_text("💵 مبلغ کاهش سرمایه رو وارد کن:")
        return

    if context.user_data.get("capital_type"):
        try:
            amount = float(t.replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("لطفاً یک مبلغ مثبت وارد کن؛ مثلاً 100")
            return

        uid = update.effective_user.id
        typ = context.user_data["capital_type"]
        conn = db()
        conn.execute(
            """INSERT INTO capital_adjustments
               (user_id, created_at, adjustment_type, amount, note)
               VALUES (?,?,?,?,?)""",
            (uid, datetime.now().isoformat(timespec="seconds"), typ, amount, None)
        )
        conn.commit()
        conn.close()
        context.user_data.pop("capital_type", None)

        bal = current_balance(uid)
        label = "افزایش" if typ == "DEPOSIT" else "کاهش"
        await update.message.reply_text(
            f"✅ {label} سرمایه به مبلغ <b>${amount:.2f}</b> ثبت شد.\n\n"
            f"🏦 سرمایه فعلی: <b>${bal:.2f}</b>\n"
            "📊 این مورد به‌عنوان معامله حساب نمی‌شود و آمار معاملاتی را تغییر نمی‌دهد.",
            parse_mode="HTML", reply_markup=main_keyboard()
        )
        return

    await update.message.reply_text("یکی از گزینه‌ها رو انتخاب کن.")

async def settings(update, context):
    bal = initial_balance(update.effective_user.id)
    await update.message.reply_text(
        f"⚙️ <b>تنظیمات</b>\n\nنسخه: <code>{VERSION}</code>\n"
        f"💰 سرمایه اولیه: ${bal or 0:.2f}\n\n"
        "🟡 XAUUSD: 0.01 lot → هر $1 حرکت = $1\n"
        "🔵 NAS100: 0.01 lot → هر 100 واحد = $1\n"
        "🟣 US30: 0.01 lot → هر 100 واحد = $1",
        parse_mode="HTML"
    )

async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ ثبت معامله لغو شد.", reply_markup=main_keyboard())
    return ConversationHandler.END

# -------------------- Main --------------------
def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    db()
    app = Application.builder().token(TOKEN).build()

    states = {
        BALANCE_SETUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_balance)],
        SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, symbol)],
        OTHER_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, other_symbol)],
        SIDE: [MessageHandler(filters.TEXT & ~filters.COMMAND, side)],
        ENTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, entry)],
        SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, sl)],
        TP: [MessageHandler(filters.TEXT & ~filters.COMMAND, tp)],
        LOT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lot)],
    }
    # Keep the whole checklist in ONE conversation state. This avoids the
    # Telegram ConversationHandler dropping the state after the first inline
    # button press. The callback itself advances check_idx until completion.
    states[CHECKLIST_START] = [
        CallbackQueryHandler(checklist_button, pattern=r"^check_(yes|no)$")
    ]
    states[RESULT] = [CallbackQueryHandler(result_callback, pattern=r"^result_(WIN|LOSS|BE)$")]
    states[EXIT_PRICE] = [MessageHandler(filters.TEXT & ~filters.COMMAND, exit_price)]
    states[FINAL_MOVE] = [MessageHandler(filters.TEXT & ~filters.COMMAND, final_move)]
    states[SCREENSHOT] = [
        MessageHandler(filters.PHOTO, screenshot_photo),
        MessageHandler(filters.TEXT & ~filters.COMMAND, screenshot_choice),
    ]

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_trade),
            MessageHandler(filters.Regex("^📝 معامله جدید$"), new_trade),
        ],
        states=states,
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("checklist", checklist_report))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("records", records))
    app.add_handler(CommandHandler("symbols", symbols))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Regex("^📊 داشبورد$"), dashboard))
    app.add_handler(MessageHandler(filters.Regex("^📅 امروز$"), today))
    app.add_handler(MessageHandler(filters.Regex("^📈 هفته$"), week))
    app.add_handler(MessageHandler(filters.Regex("^🗓 ماه$"), month))
    app.add_handler(MessageHandler(filters.Regex("^📋 تحلیل چک‌لیست$"), checklist_report))
    app.add_handler(MessageHandler(filters.Regex("^🏆 رکوردها$"), records))
    app.add_handler(MessageHandler(filters.Regex("^🔎 نمادها$"), symbols))
    app.add_handler(MessageHandler(
        filters.Regex("^(💰 مدیریت سرمایه|➕ افزایش سرمایه|➖ کاهش سرمایه|📊 سرمایه فعلی|↩️ بازگشت)$"),
        capital_menu
    ))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ تنظیمات$"), settings))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"^(?:\d+(?:[\.,]\d+)?|➕ افزایش سرمایه|➖ کاهش سرمایه|📊 سرمایه فعلی|↩️ بازگشت)$"),
        capital_adjustment
    ))
    # Initial balance entry when explicitly requested.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, setup_balance))

    port = int(os.getenv("PORT","10000"))
    external = os.getenv("RENDER_EXTERNAL_URL","").rstrip("/")
    if external:
        app.run_webhook(
            listen="0.0.0.0", port=port, url_path="telegram",
            webhook_url=f"{external}/telegram", drop_pending_updates=True
        )
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
