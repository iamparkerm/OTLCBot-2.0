import os
import asyncio
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import Bot


# ---------- Config / env ----------
ROOT = Path(__file__).resolve().parents[1]  # .../OTLCBot-2.0
load_dotenv(dotenv_path=ROOT / ".env")

DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data.db")).expanduser().resolve()
if not DB_PATH.exists():
    raise FileNotFoundError(f"DB not found at {DB_PATH}")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ENABLE_AI_SUMMARY = os.getenv("ENABLE_AI_SUMMARY", "false").lower() == "true"

# ---------- Helpers ----------
def get_weekly_snippets(conn: sqlite3.Connection, chat_id: int, since_iso: str, limit: int = 30) -> str:
    rows = conn.execute(
        """
        SELECT username, text
        FROM messages
        WHERE chat_id = ?
          AND sent_at_utc >= ?
          AND text IS NOT NULL
          AND LENGTH(TRIM(text)) BETWEEN 20 AND 200
        ORDER BY RANDOM()
        LIMIT ?;
        """,
        (chat_id, since_iso, limit),
    ).fetchall()

    snippets = []
    for username, text in rows:
        if not text:
            continue
        if username:
            snippets.append(f"{username}: {text[:200]}")
        else:
            snippets.append(text[:200])

    return "\n".join(snippets)


def generate_ai_recap(snippets: str) -> str:
    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                "You are a fun group chat summarizer. Based on these message snippets "
                "from the past week, write a casual 3-4 sentence recap of what the group "
                "was chatting about. After congratulating the group on wrapping the work week, "
                "welcome the group chat to the upcoming weekend, then be brief and lighthearted "
                "with the recap.\n\n"
                f"{snippets}"
            ),
            config={"max_output_tokens": 150},
        )
        return response.text.strip() if response.text else ""
    except Exception as e:
        print(f"AI recap failed: {e}")
        return ""


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
            SELECT COALESCE(username, full_name, 'unknown') AS who, COUNT(*) AS cnt
            FROM messages
            WHERE chat_id = ? AND sent_at_utc >= ?
            GROUP BY who
            ORDER BY cnt DESC
            LIMIT 10;
            """,
            (chat_id, since),
        ).fetchall()

        lines = [
            "📆 Weekly chat report",
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

        if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
            snippets = get_weekly_snippets(conn, chat_id, since)
            if snippets:
                recap = generate_ai_recap(snippets)
                if recap:
                    lines.append("")
                    lines.append("🤖 AI Recap:")
                    lines.append(recap)

    return "\n".join(lines)


async def send_weekly_async() -> None:
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
    if not CHAT_IDS:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID in .env")

    bot = Bot(token=TOKEN)
    for chat_id_str in CHAT_IDS:
        chat_id_int = int(chat_id_str)
        text = build_weekly_report(chat_id_int)
        await bot.send_message(chat_id=chat_id_int, text=text)
        print(f"Sent weekly report to {chat_id_int}")


def main() -> None:
    asyncio.run(send_weekly_async())


if __name__ == "__main__":
    main()
