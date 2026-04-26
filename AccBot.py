from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler, JobQueue
)
from telegram.error import BadRequest
from dotenv import load_dotenv
import sqlite3
import os
import json
import httpx
from datetime import date, timedelta, time
import pytz
import random

load_dotenv()
TOKEN = os.getenv("TOKEN")
TIMEZONE = "Europe/Berlin"  # Change to your timezone
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma3:1b"

HABITS = ["jogging", "gym", "no_sugar"]
HABIT_LABELS = {
    "jogging":  "🏃 Jogging",
    "gym":      "🏋️ Gym",
    "no_sugar": "🍬 No Sugar",
}
HABIT_SHORT = {
    "jogging":  "Run",
    "gym":      "Gym",
    "no_sugar": "Sugr",
}

LIFTS = ["bench_press", "pull_ups", "ohp", "squat"]
LIFT_LABELS = {
    "bench_press": "🏋️ Bench Press",
    "pull_ups":    "🔝 Pull Ups",
    "ohp":         "🙌 OHP",
    "squat":       "🦵 Squat",
}
LIFT_DISPLAY = {
    "bench_press": "Bench",
    "pull_ups":    "Pull Ups",
    "ohp":         "OHP",
    "squat":       "Squat",
}

# ConversationHandler states
CHOOSING_LIFT, ENTERING_REPS = range(2)

SHAME_MESSAGES = [
    "😤 Bro... you didn't log anything today. No jogging, no gym, no nothing. What are you doing?!",
    "🛋️ Still on the couch? Get up and log your habits!",
    "😴 Another day, another excuse? Log your habits already!",
    "🐌 Moving slower than your progress today. Log your habits!",
    "🤦 Really? Nothing logged today? Come on, you're better than this!",
    "📵 You've been ignoring me all day. Log your habits, NOW.",
    "😒 Your future self is disappointed. Log your habits before it's too late!",
]

# ---------------- DATABASE ----------------
conn = sqlite3.connect("habits.db", check_same_thread=False)
cursor = conn.cursor()

cursor.executescript("""
CREATE TABLE IF NOT EXISTS habits (
    user_id INTEGER,
    date TEXT,
    jogging INTEGER DEFAULT 0,
    gym INTEGER DEFAULT 0,
    no_sugar INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    group_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS lifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    date TEXT,
    lift TEXT,
    weight REAL,
    set1 INTEGER,
    set2 INTEGER,
    set3 INTEGER
);

""")
conn.commit()

# Migrate existing DB: add weight column if missing
try:
    cursor.execute("ALTER TABLE lifts ADD COLUMN weight REAL DEFAULT 0")
    conn.commit()
except Exception:
    pass  # Column already exists

# ---------------- DB HELPERS ----------------
def upsert_user(user):
    cursor.execute(
        "INSERT OR REPLACE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
        (user.id, user.username or "", user.first_name or "")
    )
    conn.commit()

def ensure_today_row(user_id):
    today = str(date.today())
    cursor.execute("SELECT 1 FROM habits WHERE user_id=? AND date=?", (user_id, today))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO habits (user_id, date) VALUES (?, ?)", (user_id, today)
        )
        conn.commit()
    return today

def toggle_habit(user_id, habit):
    today = ensure_today_row(user_id)
    cursor.execute(f"SELECT {habit} FROM habits WHERE user_id=? AND date=?", (user_id, today))
    row = cursor.fetchone()
    current = row[0] if row else 0
    new_value = 0 if current else 1
    cursor.execute(f"UPDATE habits SET {habit}=? WHERE user_id=? AND date=?", (new_value, user_id, today))
    conn.commit()
    return new_value

def get_status(user_id):
    today = str(date.today())
    cursor.execute(
        "SELECT jogging, gym, no_sugar FROM habits WHERE user_id=? AND date=?",
        (user_id, today)
    )
    row = cursor.fetchone()
    return dict(zip(HABITS, row)) if row else {h: 0 for h in HABITS}

def get_streak(user_id, habit):
    streak = 0
    check_date = date.today() - timedelta(days=1)
    for _ in range(365):
        cursor.execute(
            f"SELECT {habit} FROM habits WHERE user_id=? AND date=?",
            (user_id, str(check_date))
        )
        row = cursor.fetchone()
        if row and row[0]:
            streak += 1
            check_date -= timedelta(days=1)
        else:
            break
    cursor.execute(
        f"SELECT {habit} FROM habits WHERE user_id=? AND date=?",
        (user_id, str(date.today()))
    )
    row = cursor.fetchone()
    if row and row[0]:
        streak += 1
    return streak

def get_weekly_scores():
    week_ago = str(date.today() - timedelta(days=6))
    today = str(date.today())
    cursor.execute("""
        SELECT u.first_name, u.username,
               SUM(h.jogging + h.gym + h.no_sugar) as total
        FROM habits h
        JOIN users u ON h.user_id = u.user_id
        WHERE h.date BETWEEN ? AND ?
        GROUP BY h.user_id
        ORDER BY total DESC
        LIMIT 10
    """, (week_ago, today))
    return cursor.fetchall()

def get_weekly_review(user_id):
    week_ago = str(date.today() - timedelta(days=6))
    today = str(date.today())
    cursor.execute("""
        SELECT SUM(jogging), SUM(gym), SUM(no_sugar)
        FROM habits
        WHERE user_id=? AND date BETWEEN ? AND ?
    """, (user_id, week_ago, today))
    row = cursor.fetchone()
    return dict(zip(HABITS, row)) if row else {h: 0 for h in HABITS}

def get_weekly_performance_table():
    today = date.today()
    days = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
    day_strs = [str(d) for d in days]

    cursor.execute("SELECT user_id, first_name FROM users")
    users = cursor.fetchall()

    result = []
    for user_id, first_name in users:
        user_days = {}
        for d in day_strs:
            cursor.execute(
                "SELECT jogging, gym, no_sugar FROM habits WHERE user_id=? AND date=?",
                (user_id, d)
            )
            row = cursor.fetchone()
            if row:
                user_days[d] = dict(zip(HABITS, row))
            else:
                user_days[d] = {h: 0 for h in HABITS}
        result.append({"name": first_name, "days": user_days})

    return result, day_strs

def build_performance_table_message():
    table_data, day_strs = get_weekly_performance_table()

    if not table_data:
        return "📊 No data yet this week!"

    def row_total(u):
        return sum(u["days"][d].get(h, 0) for d in day_strs for h in HABITS)

    table_data = sorted(table_data, key=row_total, reverse=True)

    medals   = ["🥇", "🥈", "🥉", "4.", "5.", "6.", "7.", "8.", "9.", "10."]
    max_name = max(len(u["name"]) for u in table_data)
    max_total = len(HABITS) * len(day_strs)

    FULL  = "█"
    HALF  = "▒"
    EMPTY = "░"

    day_labels = " ".join(date.fromisoformat(d).strftime("%a")[0] for d in day_strs)

    lines = [
        "📅 <b>Weekly Progress</b>",
        f"<i>{date.fromisoformat(day_strs[0]).strftime('%b %d')} – {date.fromisoformat(day_strs[-1]).strftime('%b %d')}</i>",
        f"<code>{'':>{max_name + 2}}  {day_labels}</code>",
        "",
    ]

    for i, user in enumerate(table_data):
        total  = row_total(user)
        pct    = round(total / max_total * 100)
        medal  = medals[i] if i < len(medals) else f"{i+1}. "
        name   = user["name"]

        bar = ""
        for d in day_strs:
            done = sum(user["days"][d].get(h, 0) for h in HABITS)
            if done == 3:
                bar += FULL
            elif done > 0:
                bar += HALF
            else:
                bar += EMPTY

        padded_name = name.ljust(max_name)
        lines.append(f"{medal} <b>{name}</b>")
        lines.append(f"<code>  {padded_name}  {bar}  {pct:>3}%</code>")

    lines += [
        "",
        f"<code>{'█'} all 3  {'▒'} partial  {'░'} none</code>",
    ]

    return "\n".join(lines)

# ---------------- LIFTS DB HELPERS ----------------
def log_lift(user_id, lift, weight, set1, set2, set3):
    today = str(date.today())
    # Replace if already logged today for this lift
    cursor.execute(
        "DELETE FROM lifts WHERE user_id=? AND date=? AND lift=?",
        (user_id, today, lift)
    )
    cursor.execute(
        "INSERT INTO lifts (user_id, date, lift, weight, set1, set2, set3) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, today, lift, weight, set1, set2, set3)
    )
    conn.commit()

def get_today_lifts(user_id):
    today = str(date.today())
    cursor.execute(
        "SELECT lift, weight, set1, set2, set3 FROM lifts WHERE user_id=? AND date=? ORDER BY lift",
        (user_id, today)
    )
    rows = cursor.fetchall()
    return {row[0]: (row[1], row[2], row[3], row[4]) for row in rows}

def get_weekly_lifts_table():
    """
    Returns weekly lift data for all users.
    For each user+lift combo, shows the total reps logged this week.
    """
    today = date.today()
    days = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
    day_strs = [str(d) for d in days]
    week_ago = day_strs[0]
    today_str = day_strs[-1]

    cursor.execute("SELECT user_id, first_name FROM users")
    users = cursor.fetchall()

    result = []
    for user_id, first_name in users:
        user_lifts = {}
        for lift in LIFTS:
            cursor.execute("""
                SELECT date, weight, set1, set2, set3 FROM lifts
                WHERE user_id=? AND lift=? AND date BETWEEN ? AND ?
                ORDER BY date
            """, (user_id, lift, week_ago, today_str))
            rows = cursor.fetchall()
            if rows:
                sessions = []
                for row in rows:
                    day_label = date.fromisoformat(row[0]).strftime("%a")
                    weight = row[1] or 0
                    reps = [r for r in (row[2], row[3], row[4]) if r is not None and r > 0]
                    sessions.append((day_label, weight, reps))
                user_lifts[lift] = sessions
        if user_lifts:
            result.append({"name": first_name, "lifts": user_lifts})

    return result

def build_lifts_table_message():
    """
    Weekly lifts summary. For each user shows each lift they logged,
    with the sets per session across the week.

    Example:
      💪 Alice
        🏋️ Bench Press
          Mon: 8 | 6 | 5
          Wed: 7 | 7 | 6
        🦵 Squat
          Thu: 10 | 8 | 8
    """
    data = get_weekly_lifts_table()

    if not data:
        return "🏋️ No lifts logged this week!"

    today = date.today()
    week_start = today - timedelta(days=6)
    lines = [
        "💪 <b>Weekly Lifts Summary</b>",
        f"<i>{week_start.strftime('%b %d')} – {today.strftime('%b %d')}</i>",
        "",
    ]

    for user in data:
        lines.append(f"<b>{user['name']}</b>")
        for lift in LIFTS:
            sessions = user["lifts"].get(lift)
            if not sessions:
                continue
            lines.append(f"  {LIFT_LABELS[lift]}")
            for day_label, weight, reps in sessions:
                reps_str = " | ".join(str(r) for r in reps) if reps else "—"
                weight_str = f"{weight:g}kg" if weight else "BW"
                lines.append(f"<code>    {day_label}: {weight_str} — {reps_str}</code>")
        lines.append("")

    return "\n".join(lines).strip()

# ---------------- OLLAMA AI ----------------
async def ask_ollama(prompt: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(OLLAMA_URL, json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False
            })
            data = response.json()
            return data.get("response", "").strip()
    except Exception as e:
        print(f"Ollama error: {e}")
        return None

async def generate_weekly_review(first_name: str, counts: dict) -> str:
    total = sum(counts.values())
    max_possible = len(HABITS) * 7

    prompt = (
        f"You are a habit coach reviewing {first_name}'s week.\n"
        f"Here is their habit data for the last 7 days:\n"
        f"- Jogging: {counts.get('jogging', 0)}/7 days\n"
        f"- Gym: {counts.get('gym', 0)}/7 days\n"
        f"- No Sugar: {counts.get('no_sugar', 0)}/7 days\n"
        f"- Total: {total}/{max_possible}\n\n"
        f"Write a short personal weekly review for {first_name}. "
        f"Mention each habit specifically. "
        f"Be honest — praise what they did well, call out what they slacked on. "
        f"End with one specific piece of advice for next week. "
        f"Use emojis. Keep it under 120 words. "
        f"Do not use markdown like ** or ##. Write naturally like a text message."
    )

    result = await ask_ollama(prompt)
    if not result:
        msg = f"📊 Weekly Review — {first_name}\n\n"
        for habit in HABITS:
            count = counts.get(habit) or 0
            icon = "🔥" if count == 7 else "✅" if count >= 5 else "⚠️" if count >= 3 else "❌"
            msg += f"{icon} {HABIT_LABELS[habit]}: {count}/7\n"
        msg += f"\nTotal: {total}/{max_possible}"
        return msg
    return f"📊 *Weekly Review — {first_name}*\n\n{result}"

# ---------------- KEYBOARDS ----------------
def build_habit_keyboard(user_id):
    status = get_status(user_id)
    buttons = []
    for habit in HABITS:
        done = status.get(habit, 0)
        streak = get_streak(user_id, habit)
        streak_txt = f" 🔥{streak}" if streak > 0 else ""
        label = HABIT_LABELS[habit]
        check = "✅" if done else "⬜"
        buttons.append([
            InlineKeyboardButton(
                f"{check} {label}{streak_txt}",
                callback_data=f"done:{habit}"
            )
        ])
    buttons.append([InlineKeyboardButton("📊 My Stats", callback_data="stats")])
    buttons.append([InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")])
    buttons.append([InlineKeyboardButton("📅 Weekly Table", callback_data="weekly_table")])
    return InlineKeyboardMarkup(buttons)

def build_lift_selection_keyboard(user_id):
    """Keyboard to pick which lift to log."""
    today_lifts = get_today_lifts(user_id)
    buttons = []
    for lift in LIFTS:
        logged = lift in today_lifts
        check = "✅" if logged else "⬜"
        buttons.append([
            InlineKeyboardButton(
                f"{check} {LIFT_LABELS[lift]}",
                callback_data=f"liftpick:{lift}"
            )
        ])
    buttons.append([InlineKeyboardButton("📋 Today's Lifts", callback_data="lifts_today")])
    buttons.append([InlineKeyboardButton("💪 Weekly Lifts Table", callback_data="lifts_table")])
    return InlineKeyboardMarkup(buttons)

# ---------------- HANDLERS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    if update.effective_chat.type in ("group", "supergroup"):
        cursor.execute(
            "INSERT OR IGNORE INTO groups (group_id) VALUES (?)",
            (update.effective_chat.id,)
        )
        conn.commit()
        await update.message.reply_text(
            "✅ Group registered! I'll post daily updates and the weekly leaderboard here.\n"
            "Everyone DM me /start privately to register and track habits 👤"
        )
        return

    ensure_today_row(user.id)
    await update.message.reply_text(
        f"👋 Hey {user.first_name}! Track your daily habits below.\n"
        "Tap a habit to mark it done — tap again to unmark it.\n"
        "Streaks build automatically! 🔥\n\n"
        "Type /help to see how everything works.",
        reply_markup=build_habit_keyboard(user.id)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 *How it works*\n\n"
        "*🏃 Habits to track daily:*\n"
        "  • Jogging\n"
        "  • Gym\n"
        "  • No Sugar\n\n"
        "*✅ Logging habits:*\n"
        "DM me /track and tap the buttons to mark each habit done.\n"
        "Tap a ✅ habit again to unmark it.\n\n"
        "*🔥 Streaks:*\n"
        "Log a habit every day to build a streak. Miss a day and it resets to 0.\n\n"
        "*🏋️ Logging lifts:*\n"
        "DM me /lifts and pick a lift. Then send weight + reps as:\n"
        "`80 8 6 5` (weight in kg, then set1 set2 set3)\n"
        "Use `0` for bodyweight (e.g. Pull Ups: `0 8 6 5`)\n"
        "Lifts: Bench Press, Pull Ups, OHP, Squat.\n\n"
        "*🏆 Leaderboard:*\n"
        "Tracks total completions over the last 7 days. "
        "Type /leaderboard anytime to see the standings.\n\n"
        "*📅 Weekly Table:*\n"
        "See everyone's full week — each day and habit crossed per person. "
        "Type /table anytime to view it.\n\n"
        "*💪 Weekly Lifts Table:*\n"
        "See all logged lifts for the week. "
        "Type /liftable anytime to view it.\n\n"
        "*⏰ Reminders:*\n"
        "I'll DM you at 8PM if you haven't logged all your habits yet.\n\n"
        "*📊 Group updates:*\n"
        "Every day at 9AM I post the weekly scores in the group.\n"
        "Every Monday the full weekly leaderboard is posted.\n\n"
        "*📌 Commands:*\n"
        "/track — log today's habits\n"
        "/lifts — log today's lifts\n"
        "/leaderboard — see weekly standings\n"
        "/table — see full weekly performance table\n"
        "/liftable — see full weekly lifts table\n"
        "/remind — manually trigger reminders for everyone\n"
        "/review — send AI weekly review to everyone\n"
        "/help — show this message"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text("📲 DM me privately to track your habits!")
        return

    user = update.effective_user
    upsert_user(user)
    ensure_today_row(user.id)
    await update.message.reply_text(
        "🎯 Tap to log today's habits:\nTap again to unmark ✅",
        reply_markup=build_habit_keyboard(user.id)
    )

# ---------------- LIFT CONVERSATION ----------------
async def lifts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /lifts — show lift picker."""
    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text("📲 DM me privately to log your lifts!")
        return

    user = update.effective_user
    upsert_user(user)
    await update.message.reply_text(
        "🏋️ Which lift do you want to log today?\nTap a lift, then send your reps.",
        reply_markup=build_lift_selection_keyboard(user.id)
    )
    return CHOOSING_LIFT

async def lift_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped a lift button — ask for reps."""
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("liftpick:"):
        return CHOOSING_LIFT

    lift = query.data.split(":")[1]
    context.user_data["current_lift"] = lift

    label = LIFT_LABELS[lift]
    await query.edit_message_text(
        f"📝 Logging: <b>{label}</b>\n\n"
        "Send the weight and reps in this format:\n"
        "<code>80 8 6 5</code>  →  80kg, Set 1: 8, Set 2: 6, Set 3: 5\n"
        "Use <code>0</code> for bodyweight exercises (e.g. Pull Ups).\n\n"
        "Send /cancel to go back.",
        parse_mode="HTML"
    )
    return ENTERING_REPS

async def reps_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User sent weight + rep numbers — parse and save."""
    user = update.effective_user
    text = update.message.text.strip()

    parts = text.split()
    if len(parts) != 4 or not all(p.replace('.', '', 1).isdigit() for p in parts):
        await update.message.reply_text(
            "❌ Please send weight followed by 3 rep numbers.\n"
            "Example: <code>80 8 6 5</code>  (80kg, then reps per set)\n"
            "Use <code>0</code> for bodyweight exercises.",
            parse_mode="HTML"
        )
        return ENTERING_REPS

    weight = float(parts[0])
    set1, set2, set3 = int(parts[1]), int(parts[2]), int(parts[3])
    lift = context.user_data.get("current_lift")

    if not lift:
        await update.message.reply_text("Something went wrong. Use /lifts to start again.")
        return ConversationHandler.END

    log_lift(user.id, lift, weight, set1, set2, set3)
    label = LIFT_LABELS[lift]
    total = set1 + set2 + set3
    weight_str = f"{weight:g}kg" if weight else "Bodyweight"

    await update.message.reply_text(
        f"✅ <b>{label}</b> logged!\n"
        f"<code>Weight: {weight_str}\n"
        f"Set 1: {set1} reps\n"
        f"Set 2: {set2} reps\n"
        f"Set 3: {set3} reps\n"
        f"Total: {total} reps</code>\n\n"
        "Log another lift or tap /lifts to see all lifts.",
        parse_mode="HTML",
        reply_markup=build_lift_selection_keyboard(user.id)
    )
    context.user_data.pop("current_lift", None)
    return CHOOSING_LIFT

async def lift_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("current_lift", None)
    await update.message.reply_text("❌ Lift logging cancelled. Use /lifts to start again.")
    return ConversationHandler.END

# ---------------- LEADERBOARD / TABLE COMMANDS ----------------
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_weekly_scores()
    if not rows:
        msg = "🏆 No data yet this week!"
    else:
        medals = ["🥇", "🥈", "🥉"]
        msg = "🏆 *Weekly Leaderboard* (last 7 days)\n\n"
        for i, (first_name, username, total) in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i+1}."
            name = first_name or username or "Unknown"
            msg += f"{medal} {name} — {total} ✅\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def table_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = build_performance_table_message()
    await update.message.reply_text(msg, parse_mode="HTML")

async def liftable_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the weekly lifts summary table."""
    msg = build_lifts_table_message()
    await update.message.reply_text(msg, parse_mode="HTML")

async def force_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await daily_reminder(context)
    await update.message.reply_text("✅ Reminders sent to everyone!")

async def force_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generating AI reviews... this may take a moment.")
    await weekly_review(context)
    await update.message.reply_text("✅ Weekly reviews sent to everyone!")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.message.chat.type in ("group", "supergroup"):
        await query.answer("📲 DM me privately to track your habits!", show_alert=True)
        return

    user = query.from_user
    upsert_user(user)
    await query.answer()

    data = query.data

    try:
        if data.startswith("done:"):
            habit = data.split(":")[1]
            new_value = toggle_habit(user.id, habit)
            streak = get_streak(user.id, habit)
            if new_value:
                streak_msg = f" You're on a 🔥 {streak}-day streak!" if streak > 1 else ""
                status_msg = f"✅ {HABIT_LABELS[habit]} marked as done!{streak_msg}"
            else:
                status_msg = f"↩️ {HABIT_LABELS[habit]} unmarked."
            await query.edit_message_text(
                status_msg,
                reply_markup=build_habit_keyboard(user.id)
            )

        elif data == "stats":
            status = get_status(user.id)
            msg = f"📊 *{user.first_name}'s Today*\n\n"
            for habit in HABITS:
                streak = get_streak(user.id, habit)
                done = status.get(habit, 0)
                streak_txt = f"  🔥 {streak}-day streak" if streak > 0 else ""
                msg += f"{'✅' if done else '⬜'} {HABIT_LABELS[habit]}{streak_txt}\n"
            await query.edit_message_text(
                msg,
                parse_mode="Markdown",
                reply_markup=build_habit_keyboard(user.id)
            )

        elif data == "leaderboard":
            rows = get_weekly_scores()
            if not rows:
                msg = "🏆 No data yet this week!"
            else:
                medals = ["🥇", "🥈", "🥉"]
                msg = "🏆 *Weekly Leaderboard* (last 7 days)\n\n"
                for i, (first_name, username, total) in enumerate(rows):
                    medal = medals[i] if i < 3 else f"{i+1}."
                    name = first_name or username or "Unknown"
                    msg += f"{medal} {name} — {total} ✅\n"
            await query.edit_message_text(
                msg,
                parse_mode="Markdown",
                reply_markup=build_habit_keyboard(user.id)
            )

        elif data == "weekly_table":
            msg = build_performance_table_message()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=msg,
                parse_mode="HTML"
            )

        elif data.startswith("liftpick:"):
            lift = data.split(":")[1]
            context.user_data["current_lift"] = lift
            label = LIFT_LABELS[lift]
            await query.edit_message_text(
                f"📝 Logging: <b>{label}</b>\n\n"
                "Send the weight and reps in this format:\n"
                "<code>80 8 6 5</code>  →  80kg, Set 1: 8, Set 2: 6, Set 3: 5\n"
                "Use <code>0</code> for bodyweight exercises (e.g. Pull Ups).\n\n"
                "Send /cancel to go back.",
                parse_mode="HTML"
            )

        elif data == "lifts_today":
            today_lifts = get_today_lifts(user.id)
            if not today_lifts:
                msg = "🏋️ No lifts logged today yet!"
            else:
                msg = "🏋️ <b>Today's Lifts</b>\n\n"
                for lift in LIFTS:
                    if lift in today_lifts:
                        w, s1, s2, s3 = today_lifts[lift]
                        weight_str = f"{w:g}kg" if w else "BW"
                        msg += f"{LIFT_LABELS[lift]}\n"
                        msg += f"<code>  {weight_str} — {s1} | {s2} | {s3}  (Total: {s1+s2+s3} reps)</code>\n"
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=msg,
                parse_mode="HTML"
            )

        elif data == "lifts_table":
            msg = build_lifts_table_message()
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=msg,
                parse_mode="HTML"
            )

    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise

# Handler for reps text when a lift is pending (outside conversation)
async def text_reps_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch free-text weight+reps input when user has a pending lift selected via inline button."""
    if update.effective_chat.type in ("group", "supergroup"):
        return

    lift = context.user_data.get("current_lift")
    if not lift:
        return  # Nothing pending, ignore

    user = update.effective_user
    text = update.message.text.strip()
    parts = text.split()

    if len(parts) != 4 or not all(p.replace('.', '', 1).isdigit() for p in parts):
        await update.message.reply_text(
            "❌ Please send weight followed by 3 rep numbers.\n"
            "Example: <code>80 8 6 5</code>  (80kg, then reps per set)\n"
            "Use <code>0</code> for bodyweight exercises.",
            parse_mode="HTML"
        )
        return

    weight = float(parts[0])
    set1, set2, set3 = int(parts[1]), int(parts[2]), int(parts[3])
    log_lift(user.id, lift, weight, set1, set2, set3)
    label = LIFT_LABELS[lift]
    total = set1 + set2 + set3
    weight_str = f"{weight:g}kg" if weight else "Bodyweight"

    context.user_data.pop("current_lift", None)

    await update.message.reply_text(
        f"✅ <b>{label}</b> logged!\n"
        f"<code>Weight: {weight_str}\n"
        f"Set 1: {set1} reps\n"
        f"Set 2: {set2} reps\n"
        f"Set 3: {set3} reps\n"
        f"Total: {total} reps</code>\n\n"
        "Log another lift or tap /lifts to see all lifts.",
        parse_mode="HTML",
        reply_markup=build_lift_selection_keyboard(user.id)
    )

# ---------------- SCHEDULED JOBS ----------------
async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT user_id, first_name FROM users")
    users = cursor.fetchall()
    for user_id, first_name in users:
        status = get_status(user_id)
        undone = [HABIT_LABELS[h] for h in HABITS if not status.get(h, 0)]
        if not undone:
            continue
        undone_txt = "\n".join(f"  ⬜ {h}" for h in undone)
        all_undone = len(undone) == len(HABITS)
        if all_undone:
            shame = random.choice(SHAME_MESSAGES)
            text = (
                f"{shame}\n\n"
                f"You still haven't logged:\n{undone_txt}\n\n"
                "Tap below to redeem yourself 👇"
            )
        else:
            text = (
                f"⏰ *Almost there, {first_name}!*\n\n"
                f"You still haven't logged:\n{undone_txt}\n\n"
                "Tap below to finish strong 👇"
            )
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=build_habit_keyboard(user_id)
            )
        except Exception:
            pass

async def weekly_review(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT user_id, first_name FROM users")
    users = cursor.fetchall()
    for user_id, first_name in users:
        counts = get_weekly_review(user_id)
        msg = await generate_weekly_review(first_name, counts)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=msg,
                parse_mode="Markdown"
            )
        except Exception:
            pass

async def daily_group_update(context: ContextTypes.DEFAULT_TYPE):
    rows = get_weekly_scores()
    medals = ["🥇", "🥈", "🥉"]
    msg = "🌅 *Daily Habit Update!*\n\nThis week's scores so far:\n"
    if not rows:
        msg += "_No data yet — start logging! 💪_"
    else:
        for i, (first_name, username, total) in enumerate(rows[:5]):
            medal = medals[i] if i < 3 else f"{i+1}."
            name = first_name or username or "Unknown"
            msg += f"{medal} {name} — {total} ✅\n"
    msg += "\nLog today's habits by DM'ing me /track 💪"

    cursor.execute("SELECT group_id FROM groups")
    for (gid,) in cursor.fetchall():
        try:
            await context.bot.send_message(chat_id=gid, text=msg, parse_mode="Markdown")
        except Exception as e:
            print(f"Failed to message group {gid}: {e}")

async def weekly_broadcast(context: ContextTypes.DEFAULT_TYPE):
    rows = get_weekly_scores()
    if not rows:
        return
    medals = ["🥇", "🥈", "🥉"]
    msg = "🏆 *Weekly Habit Leaderboard!*\n\n"
    for i, (first_name, username, total) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = first_name or username or "Unknown"
        msg += f"{medal} {name} — {total} ✅\n"
    msg += "\nKeep it up this week! 💪"

    cursor.execute("SELECT group_id FROM groups")
    for (gid,) in cursor.fetchall():
        try:
            await context.bot.send_message(chat_id=gid, text=msg, parse_mode="Markdown")
        except Exception:
            pass

async def weekly_table_broadcast(context: ContextTypes.DEFAULT_TYPE):
    """Post the full weekly performance table to all groups every Sunday at 21:00."""
    msg = build_performance_table_message()
    cursor.execute("SELECT group_id FROM groups")
    for (gid,) in cursor.fetchall():
        try:
            await context.bot.send_message(chat_id=gid, text=msg, parse_mode="HTML")
        except Exception as e:
            print(f"Failed to send weekly table to group {gid}: {e}")

async def weekly_lifts_broadcast(context: ContextTypes.DEFAULT_TYPE):
    """Post the weekly lifts summary to all groups every Sunday at 21:10."""
    msg = build_lifts_table_message()
    cursor.execute("SELECT group_id FROM groups")
    for (gid,) in cursor.fetchall():
        try:
            await context.bot.send_message(chat_id=gid, text=msg, parse_mode="HTML")
        except Exception as e:
            print(f"Failed to send weekly lifts to group {gid}: {e}")

# ---------------- TEST COMMAND ----------------
async def test_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await daily_group_update(context)
    await update.message.reply_text("✅ Test message sent to group!")

# ---------------- RUN BOT ----------------
if __name__ == "__main__":
    tz = pytz.timezone(TIMEZONE)

    app = Application.builder().token(TOKEN).build()

    # Lift logging conversation handler
    lift_conv = ConversationHandler(
        entry_points=[CommandHandler("lifts", lifts_command)],
        states={
            CHOOSING_LIFT: [CallbackQueryHandler(lift_picked, pattern="^liftpick:")],
            ENTERING_REPS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reps_received),
            ],
        },
        fallbacks=[CommandHandler("cancel", lift_cancel)],
        per_chat=True,
        per_user=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("table", table_command))
    app.add_handler(CommandHandler("liftable", liftable_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("remind", force_remind))
    app.add_handler(CommandHandler("review", force_review))
    app.add_handler(CommandHandler("testgroup", test_group))
    app.add_handler(lift_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    # Fallback: catch reps text outside conversation (via inline button flow)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_reps_handler))

    # Daily reminder to individuals at 20:00
    app.job_queue.run_daily(
        daily_reminder,
        time=time(hour=20, minute=0, tzinfo=tz),
        name="daily_reminder"
    )

    # Daily group update at 09:00
    app.job_queue.run_daily(
        daily_group_update,
        time=time(hour=9, minute=0, tzinfo=tz),
        name="daily_group_update"
    )

    # Weekly leaderboard every Monday at 09:00
    app.job_queue.run_daily(
        weekly_broadcast,
        time=time(hour=9, minute=0, tzinfo=tz),
        days=(0,),
        name="weekly_leaderboard"
    )

    # Weekly review every Sunday at 21:00
    app.job_queue.run_daily(
        weekly_review,
        time=time(hour=21, minute=0, tzinfo=tz),
        days=(6,),
        name="weekly_review"
    )

    # Weekly performance table every Sunday at 21:05
    app.job_queue.run_daily(
        weekly_table_broadcast,
        time=time(hour=21, minute=5, tzinfo=tz),
        days=(6,),
        name="weekly_table"
    )

    # Weekly lifts table every Sunday at 21:10
    app.job_queue.run_daily(
        weekly_lifts_broadcast,
        time=time(hour=21, minute=10, tzinfo=tz),
        days=(6,),
        name="weekly_lifts"
    )

    print("✅ Bot running...")
    app.run_polling()