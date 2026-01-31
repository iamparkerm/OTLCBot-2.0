import os
import sqlite3
from datetime import timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

DB_PATH = "data.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                sent_at_utc TEXT NOT NULL,
                text TEXT
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_id, sent_at_utc);"
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ OTLCBot is running and logging messages.")


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"chat_id: {update.effective_chat.id}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(username, full_name, 'unknown') AS who, COUNT(*) AS cnt
            FROM messages
            WHERE chat_id = ?
              AND sent_at_utc >= datetime('now', '-1 day')
            GROUP BY who
            ORDER BY cnt DESC
            LIMIT 10;
            """,
            (chat_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("No messages logged in the last 24 hours.")
        return

    lines = ["📊 Top posters (last 24h):"]
    for who, cnt in rows:
        lines.append(f"- {who}: {cnt}")
    await update.message.reply_text("\n".join(lines))


async def log_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    text = msg.text or msg.caption
    if not text:
        return

    user = msg.from_user
    sent_at = msg.date
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    sent_at_utc = sent_at.astimezone(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO messages
            (chat_id, message_id, user_id, username, full_name, sent_at_utc, text)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                msg.chat_id,
                msg.message_id,
                user.id if user else None,
                user.username if user else None,
                f"{user.first_name or ''} {user.last_name or ''}".strip() if user else None,
                sent_at_utc,
                text,
            ),
        )


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")

    init_db()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT | filters.Caption, log_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
