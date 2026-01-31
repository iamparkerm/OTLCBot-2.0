import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_PATH = "data.db"


def build_weekly_report(chat_id: int) -> str:
    since_dt = datetime.now(timezone.utc) - timedelta(days=7)
    since = since_dt.isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND sent_at_utc >= ?;",
            (chat_id, since),
        ).fetchone()[0]

        top = conn.execute(
            """
            SELECT COALESCE(username, full_name, 'unknown') as who, COUNT(*) as cnt
            FROM messages
            WHERE chat_id = ? AND sent_at_utc >= ?
            GROUP BY who
            ORDER BY cnt DESC
            LIMIT 10;
            """,
            (chat_id, since),
        ).fetchall()

    lines = [
        "🗓️ Weekly chat report",
        f"Window: {since_dt.strftime('%Y-%m-%d')} → {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC)",
        f"Total messages logged: {total}",
        "",
        "🏆 Top posters:",
    ]

    if top:
        for who, cnt in top:
            lines.append(f"- {who}: {cnt}")
    else:
        lines.append("- (no messages logged)")

    return "\n".join(lines)


def send_weekly() -> None:
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
    if not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID in .env")

    chat_id_int = int(CHAT_ID)
    bot = Bot(token=TOKEN)
    text = build_weekly_report(chat_id_int)
    bot.send_message(chat_id=chat_id_int, text=text)


if __name__ == "__main__":
    send_weekly()
