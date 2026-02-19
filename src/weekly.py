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
ENABLE_SINCERITY_INDEX = os.getenv("ENABLE_SINCERITY_INDEX", "false").lower() == "true"
SINCERITY_SNIPPET_LIMIT = int(os.getenv("SINCERITY_SNIPPET_LIMIT", "50"))

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


def get_sincerity_snippets(conn: sqlite3.Connection, chat_id: int, since_iso: str, limit: int = 50) -> str:
    """Get snippets grouped by user for sincerity analysis."""
    rows = conn.execute(
        """
        SELECT COALESCE(username, full_name, 'unknown') AS who, text
        FROM messages
        WHERE chat_id = ?
          AND sent_at_utc >= ?
          AND text IS NOT NULL
          AND LENGTH(TRIM(text)) >= 10
        ORDER BY RANDOM()
        LIMIT ?;
        """,
        (chat_id, since_iso, limit),
    ).fetchall()

    snippets = []
    for who, text in rows:
        if text:
            snippets.append(f"{who}: {text[:200]}")
    return "\n".join(snippets)


def _irony_pct_to_grade(irony_pct: float) -> str:
    """Convert irony percentage to a letter grade (lower irony = better grade)."""
    sincerity = 100 - irony_pct
    if sincerity >= 93:
        return "A"
    elif sincerity >= 85:
        return "B+"
    elif sincerity >= 75:
        return "B"
    elif sincerity >= 68:
        return "B-"
    elif sincerity >= 60:
        return "C+"
    elif sincerity >= 50:
        return "C"
    elif sincerity >= 40:
        return "C-"
    elif sincerity >= 30:
        return "D"
    else:
        return "F"


def generate_sincerity_index(snippets: str) -> str:
    """Use Gemini to score irony/sincerity in the week's messages."""
    try:
        from google import genai
        import json

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                "You are a literary analyst inspired by David Foster Wallace's critique of irony "
                "in contemporary culture. Analyze these group chat messages and score the level of "
                "irony vs sincerity.\n\n"
                "For each unique user, estimate what percentage of their messages are ironic "
                "(sarcasm, cynicism, detached humor, performative disinterest, mocking tone) "
                "vs sincere (genuine, earnest, vulnerable, direct, emotionally honest).\n\n"
                "Respond ONLY with valid JSON in this exact format, no other text:\n"
                '{"group_irony_pct": <number 0-100>, "users": {"username1": <number 0-100>, "username2": <number 0-100>}}\n\n'
                "Where the numbers represent the percentage of irony detected (0 = fully sincere, "
                "100 = fully ironic).\n\n"
                f"Messages:\n{snippets}"
            ),
            config={"max_output_tokens": 300},
        )

        raw = response.text.strip() if response.text else ""
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        data = json.loads(raw)
        group_irony = float(data.get("group_irony_pct", 0))
        users = data.get("users", {})

        grade = _irony_pct_to_grade(group_irony)
        irony_int = round(group_irony)

        lines = [
            f'📖 DFW Sincerity Index: {grade}',
            f"   {irony_int}% irony detected in messages this week.",
            "",
        ]

        if users:
            # Sort by irony descending
            sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)
            lines.append("   Per-member breakdown:")
            for username, user_irony in sorted_users:
                user_grade = _irony_pct_to_grade(float(user_irony))
                lines.append(f"   - @{username}: {user_grade} ({round(float(user_irony))}% ironic)")

        lines.append("")
        lines.append(
            '   "Risk the yawn, the rolled eyes, the smarmy smile, the nudged ribs, '
            'the accusations of sentimentality and credulity." — DFW'
        )

        return "\n".join(lines)

    except Exception as e:
        print(f"Sincerity index failed: {e}")
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

        if ENABLE_SINCERITY_INDEX and GEMINI_API_KEY:
            sincerity_snippets = get_sincerity_snippets(
                conn, chat_id, since, SINCERITY_SNIPPET_LIMIT
            )
            if sincerity_snippets:
                sincerity_report = generate_sincerity_index(sincerity_snippets)
                if sincerity_report:
                    lines.append("")
                    lines.append(sincerity_report)

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
