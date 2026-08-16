
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
VERSION = "2026-08-16-pro-v4"

# -------------------- Conversation states --------------------
BALANCE_SETUP, SYMBOL, OTHER_SYMBOL, SIDE, ENTRY, SL, TP, LOT = range(8)
CHECKLIST_START = 8
RESULT = CHECKLIST_START + 18
EXIT_PRICE = RESULT + 1
FINAL_MOVE = EXIT_PRICE + 1
SCREENSHOT = FINAL_MOVE + 1

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

def fmt_pct(n, d):
    return f"{pct(n,d):.1f}%"

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
    row = conn.execute("""SELECT balance_after FROM trades WHERE user_id=? AND balance_after IS NOT NULL ORDER BY id DESC LIMIT 1""", (user_id,)).fetchone()
    adjustments = conn.execute("""SELECT COALESCE(SUM(CASE WHEN adjustment_type='DEPOSIT' THEN amount WHEN adjustment_type='WITHDRAW' THEN -amount ELSE 0 END),0) FROM capital_adjustments WHERE user_id=?""", (user_id,)).fetchone()[0]
    conn.close()
    return float(row[0]) + float(adjustments) if row else float(start) + float(adjustments)

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

# -------------------- Navigation / UX helpers --------------------
def nav_keyboard(include_back=True, include_restart=True):
    rows=[]
    if include_back and include_restart:
        rows.append([InlineKeyboardButton("🔙 مرحله قبل", callback_data="nav_back"), InlineKeyboardButton("🔄 شروع مجدد", callback_data="nav_restart")])
    elif include_back:
        rows.append([InlineKeyboardButton("🔙 مرحله قبل", callback_data="nav_back")])
    elif include_restart:
        rows.append([InlineKeyboardButton("🔄 شروع مجدد", callback_data="nav_restart")])
    return InlineKeyboardMarkup(rows) if rows else None

def merge_inline(base_buttons, context):
    rows=[list(r) for r in base_buttons]
    rows.append([InlineKeyboardButton("🔙 مرحله قبل", callback_data="nav_back"), InlineKeyboardButton("🔄 شروع مجدد", callback_data="nav_restart")])
    return InlineKeyboardMarkup(rows)

async def restart_trade(update, context):
    context.user_data.clear()
    context.user_data["checks"]=[]
    context.user_data["check_idx"]=0
    if update.callback_query:
        q=update.callback_query
        await q.answer("ثبت معامله از اول شروع شد.")
        await q.message.reply_text("🔄 از اول شروع کنیم. نماد رو انتخاب کن:", reply_markup=symbol_keyboard())
    else:
        await update.message.reply_text("🔄 از اول شروع کنیم. نماد رو انتخاب کن:", reply_markup=symbol_keyboard())
    return SYMBOL

async def nav_callback(update, context):
    q=update.callback_query
    await q.answer()
    if q.data=="nav_restart":
        return await restart_trade(update, context)
    state=context.user_data.get("flow_state")
    if state==SYMBOL:
        await q.message.reply_text("🔙 به منوی اصلی برگشتیم.", reply_markup=main_keyboard())
        context.user_data.clear(); return ConversationHandler.END
    prompts={
        SIDE:("📍 جهت معامله رو انتخاب کن:",side_keyboard(),SIDE),
        ENTRY:("🎯 نقطه ورود رو وارد کن:",None,ENTRY),
        SL:("🛑 Stop Loss رو وارد کن:",None,SL),
        TP:("🎯 Take Profit رو وارد کن:",None,TP),
        LOT:("📦 حجم معامله رو انتخاب کن:",volume_keyboard(),LOT),
        RESULT:("نتیجه معامله رو انتخاب کن:",None,RESULT),
        EXIT_PRICE:("🎯 نقطه خروج واقعی رو وارد کن:",None,EXIT_PRICE),
        FINAL_MOVE:("🚀 Final Move رو وارد کن:",None,FINAL_MOVE),
        SCREENSHOT:("📸 اسکرین‌شات معامله رو داری؟",ReplyKeyboardMarkup([["📸 آپلود عکس","🚫 عکس ندارم"]],resize_keyboard=True,one_time_keyboard=True),SCREENSHOT),
    }
    if state==CHECKLIST_START:
        stage=context.user_data.get("stage_idx",0)
        if stage>0:
            context.user_data["stage_idx"]=stage-1
            await ask_stage(q.message.chat_id,context,stage-1,message=q.message)
        else:
            await q.message.reply_text("🔙 برگشت به حجم معامله:",reply_markup=volume_keyboard())
            context.user_data["flow_state"]=LOT
            return LOT
        return CHECKLIST_START
    if state==RESULT:
        await ask_stage(q.message.chat_id,context,len(CHECKLIST)-1,message=q.message)
        context.user_data["stage_idx"]=len(CHECKLIST)-1
        return CHECKLIST_START
    prev={EXIT_PRICE:RESULT,FINAL_MOVE:EXIT_PRICE,SCREENSHOT:FINAL_MOVE}
    target=prev.get(state)
    if target:
        context.user_data["flow_state"]=target
        if target==RESULT:
            await q.message.reply_text("🔙 نتیجه معامله رو انتخاب کن:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 WIN",callback_data="result_WIN"),InlineKeyboardButton("🔴 LOSS",callback_data="result_LOSS"),InlineKeyboardButton("➖ BE",callback_data="result_BE")],[InlineKeyboardButton("🔙 مرحله قبل",callback_data="nav_back"),InlineKeyboardButton("🔄 شروع مجدد",callback_data="nav_restart")]]))
        elif target==EXIT_PRICE:
            await q.message.reply_text("🎯 نقطه خروج واقعی رو وارد کن:",reply_markup=nav_keyboard())
        elif target==FINAL_MOVE:
            await q.message.reply_text("🚀 Final Move رو وارد کن:",reply_markup=nav_keyboard())
        return target
    if state in prompts:
        text,kb,target=prompts[state]
        await q.message.reply_text(text,reply_markup=kb)
        context.user_data["flow_state"]=target
        return target
    return state

# -------------------- UI --------------------
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📝 معامله جدید", "📊 داشبورد"],
            ["📅 امروز", "📈 هفته", "🗓 ماه"],
            ["📋 تحلیل چک‌لیست", "🏆 رکوردها"],
            ["💰 مدیریت سرمایه", "🔎 نمادها"],
            ["⚙️ تنظیمات"],
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
    # This handler also receives numeric capital-adjustment amounts.
    # Handle adjustment first, otherwise the initial-balance flow works normally.
    if context.user_data.get("capital_mode"):
        t = update.message.text.strip()
        try:
            amount = float(t.replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("فقط یک مبلغ مثبت وارد کن؛ مثلاً 100")
            return
        uid = update.effective_user.id
        typ = context.user_data.pop("capital_mode")
        conn = db()
        conn.execute(
            "INSERT INTO capital_adjustments(user_id,created_at,adjustment_type,amount,note) VALUES(?,?,?,?,?)",
            (uid, datetime.now().isoformat(timespec="seconds"), typ, amount, None)
        )
        conn.commit()
        conn.close()
        sign = "افزایش" if typ == "DEPOSIT" else "کاهش"
        await update.message.reply_text(
            f"✅ {sign} سرمایه به مبلغ <b>${amount:.2f}</b> ثبت شد.\n"
            f"🏦 سرمایه فعلی: <b>${current_balance(uid):.2f}</b>",
            parse_mode="HTML", reply_markup=main_keyboard()
        )
        return

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
    context.user_data["flow_state"]=SIDE
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
    await update.message.reply_text("🎯 نقطه ورود رو وارد کن:", reply_markup=nav_keyboard())
    context.user_data["flow_state"]=ENTRY
    return ENTRY

async def number_field(update, context, key, prompt, state):
    try:
        v = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("لطفاً فقط عدد وارد کن.")
        return state
    context.user_data[key] = v
    await update.message.reply_text(prompt, reply_markup=nav_keyboard())
    context.user_data["flow_state"]=state+1
    return state + 1

async def entry(update, context):
    return await number_field(update, context, "entry", "🛑 Stop Loss رو وارد کن:", ENTRY)

async def sl(update, context):
    return await number_field(update, context, "sl", "🎯 Take Profit رو وارد کن:", SL)

async def tp(update, context):
    await update.message.reply_text(
        "📦 حجم معامله رو انتخاب کن:", reply_markup=volume_keyboard()
    )
    context.user_data["flow_state"]=LOT
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
    await update.message.reply_text(
        f"📐 R:R برنامه‌ریزی‌شده: <b>1:{context.user_data['planned_rr']:.2f}</b>\n\n"
        "حالا چک‌لیست ۶ مرحله‌ای رو بررسی کنیم ☑️",
        parse_mode="HTML", reply_markup=ReplyKeyboardRemove()
    )
    context.user_data["stage_idx"]=0
    await ask_check(update, context)
    context.user_data["flow_state"]=CHECKLIST_START
    return CHECKLIST_START

def stage_item_indices(stage_idx):
    start=sum(len(CHECKLIST[i][1]) for i in range(stage_idx))
    return list(range(start,start+len(CHECKLIST[stage_idx][1])))

def stage_keyboard(context, stage_idx):
    checks=context.user_data.setdefault("checks",[False]*TOTAL_CHECKS)
    rows=[]
    for local,q in enumerate(CHECKLIST[stage_idx][1]):
        gi=stage_item_indices(stage_idx)[local]
        mark="☑️" if checks[gi] else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {q}",callback_data=f"toggle_{gi}")])
    rows.append([InlineKeyboardButton(f"✅ ثبت مرحله {stage_idx+1}",callback_data=f"stage_done_{stage_idx}")])
    rows.append([InlineKeyboardButton("🔙 مرحله قبل",callback_data="nav_back"),InlineKeyboardButton("🔄 شروع مجدد",callback_data="nav_restart")])
    return InlineKeyboardMarkup(rows)

async def ask_stage(chat_id,context,stage_idx,message=None):
    section,_=CHECKLIST[stage_idx]
    checks=context.user_data.setdefault("checks",[False]*TOTAL_CHECKS)
    inds=stage_item_indices(stage_idx)
    selected=sum(checks[i] for i in inds)
    text=f"<b>{section}</b>\n\nهر موردی که برقرار است را تیک بزن. ☑️\nانتخاب‌شده: <b>{selected}/{len(inds)}</b>"
    kb=stage_keyboard(context,stage_idx)
    if message:
        await message.edit_text(text,parse_mode="HTML",reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=chat_id,text=text,parse_mode="HTML",reply_markup=kb)

async def ask_check(update,context):
    await ask_stage(update.effective_chat.id,context,context.user_data.get("stage_idx",0))

async def checklist_button(update,context):
    q=update.callback_query; await q.answer()
    if q.data.startswith("toggle_"):
        idx=int(q.data.split("_",1)[1]); checks=context.user_data.setdefault("checks",[False]*TOTAL_CHECKS)
        checks[idx]=not checks[idx]
        await ask_stage(q.message.chat_id,context,context.user_data.get("stage_idx",0),q.message)
        return CHECKLIST_START
    if q.data.startswith("stage_done_"):
        done=int(q.data.rsplit("_",1)[1]); stage=context.user_data.get("stage_idx",0)
        if done!=stage: return CHECKLIST_START
        selected=sum(context.user_data["checks"][i] for i in stage_item_indices(done))
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        if done<len(CHECKLIST)-1:
            await q.message.reply_text(f"✅ مرحله {done+1} ثبت شد — {selected}/{len(CHECKLIST[done][1])} مورد تأیید شد.")
            context.user_data["stage_idx"]=done+1
            await ask_stage(q.message.chat_id,context,done+1)
            return CHECKLIST_START
        total_selected=sum(context.user_data["checks"])
        context.user_data["flow_state"]=RESULT
        await q.message.reply_text(f"🏁 چک‌لیست کامل شد: <b>{total_selected}/{TOTAL_CHECKS}</b> ({pct(total_selected,TOTAL_CHECKS):.1f}%)\n\nنتیجه معامله رو انتخاب کن:",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🟢 WIN",callback_data="result_WIN"),InlineKeyboardButton("🔴 LOSS",callback_data="result_LOSS"),InlineKeyboardButton("➖ BE",callback_data="result_BE")],[InlineKeyboardButton("🔙 مرحله قبل",callback_data="nav_back"),InlineKeyboardButton("🔄 شروع مجدد",callback_data="nav_restart")]]))
        return RESULT
    return CHECKLIST_START

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
    context.user_data["flow_state"]=FINAL_MOVE
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
    context.user_data["flow_state"]=SCREENSHOT
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
        f"🎯 Win Rate: <b>{fmt_pct(wins,total)}</b>\n"
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
    wins=[r for r in rows if str(r[14]).strip().upper()=="WIN"]
    losses=[r for r in rows if str(r[14]).strip().upper()=="LOSS"]
    lines=["📋 <b>تحلیل چک‌لیست — تفکیک WIN و LOSS</b>"]
    offset=0
    for section,questions in CHECKLIST:
        lines.append(f"\n<b>{section}</b>")
        for question in questions:
            def item_rate(group, want_checked):
                if not group: return None
                vals=[r[12].split(",") for r in group]
                vals=[v for v in vals if len(v)>offset]
                if not vals: return None
                hits=sum((v[offset]=="1") == want_checked for v in vals)
                return pct(hits,len(vals))
            wr=item_rate(wins,True); lr=item_rate(losses,False)
            wtxt="—" if wr is None else f"{wr:.0f}%"
            ltxt="—" if lr is None else f"{lr:.0f}%"
            lines.append(f"• {question}\n  🟢 در WIN تیک خورده: <b>{wtxt}</b> | 🔴 در LOSS تیک نخورده: <b>{ltxt}</b>")
            offset+=1
    if not wins or not losses: lines.append("\nℹ️ برای مقایسه کامل حداقل یک WIN و یک LOSS لازم است.")
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
    await update.message.reply_text("💰 <b>مدیریت سرمایه</b>\n\nواریز و برداشت جدا از معاملات ثبت می‌شود و آمار معاملاتی را تغییر نمی‌دهد.",parse_mode="HTML",reply_markup=ReplyKeyboardMarkup([["➕ افزایش سرمایه","➖ کاهش سرمایه"],["📊 سرمایه فعلی","📜 تاریخچه"],["🔙 بازگشت"]],resize_keyboard=True))

async def capital_action(update, context):
    t=update.message.text.strip()
    uid=update.effective_user.id
    if t=="🔙 بازگشت":
        context.user_data.pop("capital_mode",None); await update.message.reply_text("برگشتیم به منوی اصلی 😎",reply_markup=main_keyboard()); return
    if t=="📊 سرمایه فعلی":
        await update.message.reply_text(f"🏦 سرمایه فعلی: <b>${current_balance(uid):.2f}</b>",parse_mode="HTML",reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]],resize_keyboard=True)); return
    if t=="📜 تاریخچه":
        conn=db(); rows=conn.execute("SELECT created_at,adjustment_type,amount,note FROM capital_adjustments WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall(); conn.close()
        if not rows: text="📜 هنوز تغییر سرمایه‌ای ثبت نشده."
        else:
            lines=["📜 <b>تاریخچه تغییرات سرمایه</b>"]
            for dt,typ,amt,note in rows: lines.append(f"{('➕' if typ=='DEPOSIT' else '➖')} ${amt:.2f} — {dt[:16]}")
            text="\n".join(lines)
        await update.message.reply_text(text,parse_mode="HTML",reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]],resize_keyboard=True)); return
    if t in ("➕ افزایش سرمایه","➖ کاهش سرمایه"):
        context.user_data["capital_mode"]="DEPOSIT" if t.startswith("➕") else "WITHDRAW"
        await update.message.reply_text("💵 مبلغ رو وارد کن:",reply_markup=ReplyKeyboardMarkup([["🔙 بازگشت"]],resize_keyboard=True)); return
    if context.user_data.get("capital_mode"):
        try: amount=float(t.replace(",",".")); assert amount>0
        except: await update.message.reply_text("فقط یک مبلغ مثبت وارد کن؛ مثلاً 100"); return
        typ=context.user_data.pop("capital_mode")
        conn=db(); conn.execute("INSERT INTO capital_adjustments(user_id,created_at,adjustment_type,amount,note) VALUES(?,?,?,?,?)",(uid,datetime.now().isoformat(timespec="seconds"),typ,amount,None)); conn.commit(); conn.close()
        sign="افزایش" if typ=="DEPOSIT" else "کاهش"
        await update.message.reply_text(f"✅ {sign} سرمایه به مبلغ <b>${amount:.2f}</b> ثبت شد.\n🏦 سرمایه فعلی: <b>${current_balance(uid):.2f}</b>",parse_mode="HTML",reply_markup=main_keyboard()); return
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

    nav_all = CallbackQueryHandler(nav_callback, pattern=r"^nav_(back|restart)$")
    states = {
        SYMBOL: [nav_all, MessageHandler(filters.TEXT & ~filters.COMMAND, symbol)],
        OTHER_SYMBOL: [nav_all, MessageHandler(filters.TEXT & ~filters.COMMAND, other_symbol)],
        SIDE: [nav_all, MessageHandler(filters.TEXT & ~filters.COMMAND, side)],
        ENTRY: [nav_all, MessageHandler(filters.TEXT & ~filters.COMMAND, entry)],
        SL: [nav_all, MessageHandler(filters.TEXT & ~filters.COMMAND, sl)],
        TP: [nav_all, MessageHandler(filters.TEXT & ~filters.COMMAND, tp)],
        LOT: [nav_all, MessageHandler(filters.TEXT & ~filters.COMMAND, lot)],
        CHECKLIST_START: [
            CallbackQueryHandler(nav_callback, pattern=r"^nav_(back|restart)$"),
            CallbackQueryHandler(checklist_button, pattern=r"^(toggle_\\d+|stage_done_\\d+)$")
        ],
        RESULT: [
            CallbackQueryHandler(nav_callback, pattern=r"^nav_(back|restart)$"),
            CallbackQueryHandler(result_callback, pattern=r"^result_(WIN|LOSS|BE)$")
        ],
        EXIT_PRICE: [nav_all, MessageHandler(filters.TEXT & ~filters.COMMAND, exit_price)],
        FINAL_MOVE: [nav_all, MessageHandler(filters.TEXT & ~filters.COMMAND, final_move)],
        SCREENSHOT: [
            nav_all,
            MessageHandler(filters.PHOTO, screenshot_photo),
            MessageHandler(filters.TEXT & ~filters.COMMAND, screenshot_choice),
        ],
    }

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
    app.add_handler(MessageHandler(filters.Regex("^⚙️ تنظیمات$"), settings))
    app.add_handler(MessageHandler(filters.Regex("^💰 مدیریت سرمایه$"), capital_menu))
    app.add_handler(MessageHandler(filters.Regex("^(?:➕ افزایش سرمایه|➖ کاهش سرمایه|📊 سرمایه فعلی|📜 تاریخچه|🔙 بازگشت)$"), capital_action))
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
