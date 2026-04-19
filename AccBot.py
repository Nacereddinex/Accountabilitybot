from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)
from dotenv import load_dotenv
import sqlite3
import os
from datetime import date, timedelta, time
import pytz

load_dotenv()
TOKEN = os.getenv("TOKEN")
TIMEZONE = "Europe/Berlin"  # Change to your timezone

HABITS = ["jogging", "gym", "no_sugar"]
HABIT_LABELS = {
    "jogging":  "🏃 Jogging",
    "gym":      "🏋️ Gym",
    "no_sugar": "🍬 No Sugar",
}

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
""")
conn.commit()

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

def mark_done(user_id, habit):
    today = ensure_today_row(user_id)
    cursor.execute(f"UPDATE habits SET {habit}=1 WHERE user_id=? AND date=?", (user_id, today))
    conn.commit()

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
        "Tap a habit to mark it done — streaks build automatically! 🔥",
        reply_markup=build_habit_keyboard(user.id)
    )

async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text("📲 DM me privately to track your habits!")
        return

    user = update.effective_user
    upsert_user(user)
    ensure_today_row(user.id)
    await update.message.reply_text(
        "🎯 Tap to log today's habits:",
        reply_markup=build_habit_keyboard(user.id)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.message.chat.type in ("group", "supergroup"):
        await query.answer("📲 DM me privately to track your habits!", show_alert=True)
        return

    user = query.from_user
    upsert_user(user)
    await query.answer()

    data = query.data

    if data.startswith("done:"):
        habit = data.split(":")[1]
        mark_done(user.id, habit)
        streak = get_streak(user.id, habit)
        streak_msg = f" You're on a 🔥 {streak}-day streak!" if streak > 1 else ""
        await query.edit_message_text(
            f"✅ {HABIT_LABELS[habit]} marked as done!{streak_msg}",
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

# ---------------- SCHEDULED JOBS ----------------
async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT user_id FROM users")
    user_ids = [r[0] for r in cursor.fetchall()]
    for user_id in user_ids:
        status = get_status(user_id)
        undone = [HABIT_LABELS[h] for h in HABITS if not status.get(h, 0)]
        if not undone:
            continue
        undone_txt = "\n".join(f"  ⬜ {h}" for h in undone)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⏰ *Daily Check-in!*\n\n"
                    f"You still haven't logged:\n{undone_txt}\n\n"
                    "Tap below to mark them done 👇"
                ),
                parse_mode="Markdown",
                reply_markup=build_habit_keyboard(user_id)
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

# ---------------- TEST COMMAND ----------------
async def test_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await daily_group_update(context)
    await update.message.reply_text("✅ Test message sent to group!")

# ---------------- RUN BOT ----------------
if __name__ == "__main__":
    tz = pytz.timezone(TIMEZONE)

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("testgroup", test_group))
    app.add_handler(CallbackQueryHandler(button_handler))

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

    print("✅ Bot running...")
    app.run_polling()