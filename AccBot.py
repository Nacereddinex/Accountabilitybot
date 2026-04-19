from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, JobQueue
)
from telegram.error import BadRequest
from dotenv import load_dotenv
import sqlite3
import os
from datetime import date, timedelta, time
import pytz
import random

load_dotenv()
TOKEN = os.getenv("TOKEN")
TIMEZONE = "Europe/Berlin"  # Change to your timezone

HABITS = ["jogging", "gym", "no_sugar"]
HABIT_LABELS = {
    "jogging":  "🏃 Jogging",
    "gym":      "🏋️ Gym",
    "no_sugar": "🍬 No Sugar",
}

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
    """Returns per-habit counts for the past 7 days for a single user."""
    week_ago = str(date.today() - timedelta(days=6))
    today = str(date.today())
    cursor.execute("""
        SELECT
            SUM(jogging), SUM(gym), SUM(no_sugar)
        FROM habits
        WHERE user_id=? AND date BETWEEN ? AND ?
    """, (user_id, week_ago, today))
    row = cursor.fetchone()
    return dict(zip(HABITS, row)) if row else {h: 0 for h in HABITS}

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
        "*🏆 Leaderboard:*\n"
        "Tracks total completions over the last 7 days. "
        "Type /leaderboard anytime to see the standings.\n\n"
        "*⏰ Reminders:*\n"
        "I'll DM you at 8PM if you haven't logged all your habits yet.\n\n"
        "*📊 Group updates:*\n"
        "Every day at 9AM I post the weekly scores in the group.\n"
        "Every Monday the full weekly leaderboard is posted.\n\n"
        "*📌 Commands:*\n"
        "/track — log today's habits\n"
        "/leaderboard — see weekly standings\n"
        "/remind — manually trigger reminders for everyone\n"
        "/review — send weekly review to everyone\n"
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

async def force_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await daily_reminder(context)
    await update.message.reply_text("✅ Reminders sent to everyone!")

async def force_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise

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

        # Pick a random shame message if nothing logged at all, else normal reminder
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
    """Send each user a private weekly review every Sunday night."""
    cursor.execute("SELECT user_id, first_name FROM users")
    users = cursor.fetchall()

    for user_id, first_name in users:
        counts = get_weekly_review(user_id)
        total = sum(counts.values())
        max_possible = len(HABITS) * 7

        msg = f"📊 *Weekly Review — {first_name}*\n\n"
        msg += "Here's how you did this week:\n\n"

        for habit in HABITS:
            count = counts.get(habit) or 0
            if count == 7:
                icon = "🔥"
                comment = "Perfect week!"
            elif count >= 5:
                icon = "✅"
                comment = "Solid!"
            elif count >= 3:
                icon = "⚠️"
                comment = "Could be better"
            else:
                icon = "❌"
                comment = "Come on..."
            msg += f"{icon} {HABIT_LABELS[habit]}: {count}/7 — {comment}\n"

        msg += f"\n*Total: {total}/{max_possible}*\n"

        if total == max_possible:
            msg += "\n🏆 Perfect week! You're an absolute beast!"
        elif total >= max_possible * 0.75:
            msg += "\n💪 Great week overall — keep pushing!"
        elif total >= max_possible * 0.5:
            msg += "\n😐 Decent week but you can do better. Step it up!"
        else:
            msg += "\n😤 Rough week. No excuses next week — get your act together!"

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
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("remind", force_remind))
    app.add_handler(CommandHandler("review", force_review))
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

    # Weekly review every Sunday at 21:00
    app.job_queue.run_daily(
        weekly_review,
        time=time(hour=21, minute=0, tzinfo=tz),
        days=(6,),  # 6 = Sunday
        name="weekly_review"
    )

    print("✅ Bot running...")
    app.run_polling()