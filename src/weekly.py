import io
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

# Owl Town combined summary: multiple groups aggregated into one report
OWL_TOWN_CHAT_IDS = [cid.strip() for cid in os.getenv("OWL_TOWN_CHAT_IDS", "").split(",") if cid.strip()]
OWL_TOWN_SEND_TO = os.getenv("OWL_TOWN_SEND_TO", "")  # chat_id to send combined report to
OWL_TOWN_NAMES = {}  # map chat_id -> friendly name
for pair in os.getenv("OWL_TOWN_NAMES", "").split(","):
    if "=" in pair:
        cid, name = pair.split("=", 1)
        OWL_TOWN_NAMES[cid.strip()] = name.strip()

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
                "You are a group chat summarizer. Based on these message snippets "
                "from the past week, write a straightforward 3-4 sentence recap of what the group "
                "was chatting about. Be brief and genuine — no forced enthusiasm or cheerfulness.\n\n"
                f"{snippets}"
            ),
            config={"max_output_tokens": 150},
        )
        return response.text.strip() if response.text else ""
    except Exception as e:
        print(f"AI recap failed: {e}")
        return ""


def generate_weekly_image(snippets: str) -> bytes | None:
    """
    Generate a weekly illustration from conversation snippets.
    Returns raw image bytes or None on failure.
    """
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)
        # Step 1: ask the text model to write a vivid image prompt from the snippets
        prompt_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "Based on these group chat snippets from the past week, write a single "
                "sentence describing a fun, illustrated scene that captures the week's vibe. "
                "Be specific and visual. No more than 30 words.\n\n"
                f"{snippets}"
            ),
            config={"max_output_tokens": 60},
        )
        image_prompt = prompt_response.text.strip()
        image_prompt += ", New Yorker cartoon style, single panel, loose ink illustration, subtle humor"
        print(f"  Image prompt: {image_prompt}")

        # Step 2: generate the image
        image_response = client.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=image_prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"]
            ),
        )
        for part in image_response.parts:
            if part.inline_data is not None:
                return part.inline_data.data  # raw bytes
        return None
    except Exception as e:
        print(f"Image generation failed: {e}")
        return None


def get_user_snippets(conn: sqlite3.Connection, chat_id: int, username: str, since_iso: str, limit: int = 20) -> str:
    """Get message snippets for a single user."""
    rows = conn.execute(
        """
        SELECT text
        FROM messages
        WHERE chat_id = ?
          AND username = ?
          AND sent_at_utc >= ?
          AND text IS NOT NULL
          AND LENGTH(TRIM(text)) >= 10
        ORDER BY RANDOM()
        LIMIT ?;
        """,
        (chat_id, username, since_iso, limit),
    ).fetchall()
    return "\n".join(row[0][:200] for row in rows if row[0])


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


def _get_last_week_group_grade(conn: sqlite3.Connection, chat_id: int, current_week: str) -> str | None:
    """Get the overall group grade from the previous week."""
    row = conn.execute(
        """
        SELECT week_of, irony_pct FROM sincerity_scores
        WHERE chat_id = ? AND username = '__group__' AND week_of < ?
        ORDER BY week_of DESC LIMIT 1;
        """,
        (chat_id, current_week),
    ).fetchone()
    if row:
        return _irony_pct_to_grade(row[1])
    return None


def _get_last_week_user_score(conn: sqlite3.Connection, chat_id: int, username: str, current_week: str) -> tuple[str | None, float | None]:
    """Get a user's grade and irony % from the previous week."""
    row = conn.execute(
        """
        SELECT irony_pct FROM sincerity_scores
        WHERE chat_id = ? AND username = ? AND week_of < ?
        ORDER BY week_of DESC LIMIT 1;
        """,
        (chat_id, username, current_week),
    ).fetchone()
    if row:
        return _irony_pct_to_grade(row[0]), row[0]
    return None, None


def _trend_arrow(current_irony: float, prev_irony: float | None) -> str:
    """Return a trend description comparing current to previous irony %."""
    if prev_irony is None:
        return "First week tracked!"
    diff = current_irony - prev_irony
    if diff < -5:
        return "Trending more sincere 🙏"
    elif diff > 5:
        return "Trending more ironic 🤔"
    else:
        return "Holding steady"


def analyze_sincerity(snippets: str) -> dict | None:
    """Use Gemini to score irony/sincerity. Returns raw data dict or None."""
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
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        return json.loads(raw)

    except Exception as e:
        print(f"Sincerity analysis failed: {e}")
        return None


def save_sincerity_scores(conn: sqlite3.Connection, chat_id: int, week_of: str, data: dict) -> None:
    """Persist this week's sincerity scores for trend tracking."""
    group_irony = float(data.get("group_irony_pct", 0))
    conn.execute(
        "INSERT INTO sincerity_scores (chat_id, week_of, username, irony_pct, grade) VALUES (?, ?, ?, ?, ?);",
        (chat_id, week_of, "__group__", group_irony, _irony_pct_to_grade(group_irony)),
    )
    for username, irony_pct in data.get("users", {}).items():
        conn.execute(
            "INSERT INTO sincerity_scores (chat_id, week_of, username, irony_pct, grade) VALUES (?, ?, ?, ?, ?);",
            (chat_id, week_of, username, float(irony_pct), _irony_pct_to_grade(float(irony_pct))),
        )


def build_group_sincerity_message(conn: sqlite3.Connection, chat_id: int, data: dict, week_of: str) -> str:
    """Build the group-facing sincerity message (trend only, no per-user)."""
    group_irony = float(data.get("group_irony_pct", 0))
    grade = _irony_pct_to_grade(group_irony)
    irony_int = round(group_irony)

    last_grade = _get_last_week_group_grade(conn, chat_id, week_of)
    if last_grade:
        trend_str = f"   Last week: {last_grade} → This week: {grade}"
    else:
        trend_str = "   First week tracked!"

    lines = [
        f"📖 DFW Sincerity Index: {grade}",
        f"   {irony_int}% irony detected in messages this week.",
        trend_str,
        "",
        '   "What passes for hip cynical transcendence of sentiment is really',
        '   some kind of fear of being really human, since to be really human',
        '   is probably to be unavoidably sentimental and naïve and goo-prone."',
        '   — Infinite Jest',
    ]
    return "\n".join(lines)


def build_user_dm(conn: sqlite3.Connection, chat_id: int, username: str, irony_pct: float, week_of: str) -> str:
    """Build a private DM for an individual user with their score + trend."""
    grade = _irony_pct_to_grade(irony_pct)
    irony_int = round(irony_pct)
    prev_grade, prev_irony = _get_last_week_user_score(conn, chat_id, username, week_of)
    trend = _trend_arrow(irony_pct, prev_irony)

    lines = [
        f"📖 Your DFW Sincerity Index: {grade}",
        f"   {irony_int}% irony detected in your messages this week.",
    ]
    if prev_grade:
        lines.append(f"   Last week: {prev_grade} → This week: {grade}. {trend}")
    else:
        lines.append(f"   {trend}")
    lines.append("")
    lines.append(
        '   "What passes for hip cynical transcendence of sentiment is really\n'
        '   some kind of fear of being really human, since to be really human\n'
        '   is probably to be unavoidably sentimental and naïve and goo-prone."\n'
        '   — Infinite Jest'
    )
    return "\n".join(lines)


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


def build_owl_town_report() -> str:
    """Build a combined weekly report across all Owl Town groups."""
    since_dt = datetime.now(timezone.utc) - timedelta(days=7)
    since = since_dt.isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        chat_ids_int = [int(cid) for cid in OWL_TOWN_CHAT_IDS]
        placeholders = ",".join("?" * len(chat_ids_int))

        # Total messages across all groups
        grand_total = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE chat_id IN ({placeholders}) AND sent_at_utc >= ?;",
            (*chat_ids_int, since),
        ).fetchone()[0]

        # Top posters across all groups
        top = conn.execute(
            f"""
            SELECT COALESCE(username, full_name, 'unknown') AS who, COUNT(*) AS cnt
            FROM messages
            WHERE chat_id IN ({placeholders}) AND sent_at_utc >= ?
            GROUP BY who
            ORDER BY cnt DESC
            LIMIT 10;
            """,
            (*chat_ids_int, since),
        ).fetchall()

        # Per-group breakdown
        per_group = conn.execute(
            f"""
            SELECT chat_id, COUNT(*) AS cnt
            FROM messages
            WHERE chat_id IN ({placeholders}) AND sent_at_utc >= ?
            GROUP BY chat_id
            ORDER BY cnt DESC;
            """,
            (*chat_ids_int, since),
        ).fetchall()

        lines = [
            "🦉 Owl Town Chats — Weekly Report",
            f"Window: {since_dt.strftime('%Y-%m-%d')} → {datetime.now(timezone.utc).strftime('%Y-%m-%d')} (UTC)",
            f"Total messages across all chats: {grand_total}",
            "",
            "💬 Per-chat breakdown:",
        ]

        for cid, cnt in per_group:
            name = OWL_TOWN_NAMES.get(str(cid), f"Chat {cid}")
            lines.append(f"- {name}: {cnt}")

        # Show any groups with 0 messages
        active_cids = {cid for cid, _ in per_group}
        for cid in chat_ids_int:
            if cid not in active_cids:
                name = OWL_TOWN_NAMES.get(str(cid), f"Chat {cid}")
                lines.append(f"- {name}: 0")

        lines.append("")
        lines.append("🏆 Top posters (all chats):")

        if top:
            for who, cnt in top:
                lines.append(f"- {who}: {cnt}")
        else:
            lines.append("- (no messages logged)")

        # Combined AI recap
        if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
            all_snippets = []
            for cid in chat_ids_int:
                s = get_weekly_snippets(conn, cid, since, limit=10)
                if s:
                    all_snippets.append(s)
            combined_snippets = "\n".join(all_snippets)
            if combined_snippets:
                recap = generate_ai_recap(combined_snippets)
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
    week_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Skip individual reports for chats that will get the Owl Town combined report
    owl_town_send_to_int = int(OWL_TOWN_SEND_TO) if OWL_TOWN_SEND_TO else None

    for chat_id_str in CHAT_IDS:
        chat_id_int = int(chat_id_str)

        # This chat gets the combined Owl Town report instead
        if owl_town_send_to_int and chat_id_int == owl_town_send_to_int:
            print(f"Skipping individual report for {chat_id_int} (will get Owl Town combined)")
            continue

        text = build_weekly_report(chat_id_int)

        # --- Sincerity Index (group trend + individual DMs) ---
        sincerity_data = None
        if ENABLE_SINCERITY_INDEX and GEMINI_API_KEY:
            since_dt = datetime.now(timezone.utc) - timedelta(days=7)
            since = since_dt.isoformat()

            with sqlite3.connect(DB_PATH) as conn:
                sincerity_snippets = get_sincerity_snippets(
                    conn, chat_id_int, since, SINCERITY_SNIPPET_LIMIT
                )
                if sincerity_snippets:
                    sincerity_data = analyze_sincerity(sincerity_snippets)

                if sincerity_data:
                    # Build group message (trend only, no per-user)
                    group_msg = build_group_sincerity_message(
                        conn, chat_id_int, sincerity_data, week_of
                    )
                    text += "\n\n" + group_msg

                    # Save scores for trend tracking (before DMs so trends work)
                    save_sincerity_scores(conn, chat_id_int, week_of, sincerity_data)

        # Generate weekly image and send
        image_bytes = None
        if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
            since_dt_img = datetime.now(timezone.utc) - timedelta(days=7)
            with sqlite3.connect(DB_PATH) as conn:
                img_snippets = get_weekly_snippets(conn, chat_id_int, since_dt_img.isoformat())
                if img_snippets:
                    image_bytes = generate_weekly_image(img_snippets)

        if image_bytes:
            await bot.send_photo(chat_id=chat_id_int, photo=io.BytesIO(image_bytes))
        await bot.send_message(chat_id=chat_id_int, text=text)
        print(f"Sent weekly report to {chat_id_int}")

        # Send individual DMs
        if sincerity_data and sincerity_data.get("users"):
            with sqlite3.connect(DB_PATH) as conn:
                # Look up user_ids for each username so we can DM them
                for username, irony_pct in sincerity_data["users"].items():
                    row = conn.execute(
                        """
                        SELECT DISTINCT user_id FROM messages
                        WHERE chat_id = ? AND username = ? AND user_id IS NOT NULL
                        ORDER BY id DESC LIMIT 1;
                        """,
                        (chat_id_int, username),
                    ).fetchone()

                    if row and row[0]:
                        dm_text = build_user_dm(
                            conn, chat_id_int, username, float(irony_pct), week_of
                        )
                        try:
                            # Generate personal cartoon from user's messages
                            if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
                                since_dm = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                                user_snippets = get_user_snippets(conn, chat_id_int, username, since_dm)
                                if user_snippets:
                                    dm_image = generate_weekly_image(user_snippets)
                                    if dm_image:
                                        await bot.send_photo(chat_id=row[0], photo=io.BytesIO(dm_image))
                            await bot.send_message(chat_id=row[0], text=dm_text)
                            print(f"  DM sent to @{username} ({row[0]})")
                        except Exception as e:
                            print(f"  DM to @{username} failed: {e}")

    # --- Owl Town combined report ---
    if OWL_TOWN_CHAT_IDS and OWL_TOWN_SEND_TO:
        owl_text = build_owl_town_report()

        # Sincerity index across all Owl Town chats
        if ENABLE_SINCERITY_INDEX and GEMINI_API_KEY:
            since_dt = datetime.now(timezone.utc) - timedelta(days=7)
            since = since_dt.isoformat()

            with sqlite3.connect(DB_PATH) as conn:
                all_snippets = []
                for cid_str in OWL_TOWN_CHAT_IDS:
                    s = get_sincerity_snippets(conn, int(cid_str), since, SINCERITY_SNIPPET_LIMIT // len(OWL_TOWN_CHAT_IDS) or 10)
                    if s:
                        all_snippets.append(s)
                combined = "\n".join(all_snippets)

                if combined:
                    sincerity_data = analyze_sincerity(combined)
                    if sincerity_data:
                        # Use a synthetic chat_id for Owl Town trend tracking
                        owl_town_id = 0  # special ID for combined
                        group_msg = build_group_sincerity_message(conn, owl_town_id, sincerity_data, week_of)
                        owl_text += "\n\n" + group_msg
                        save_sincerity_scores(conn, owl_town_id, week_of, sincerity_data)

        # Generate Owl Town weekly image
        owl_image_bytes = None
        if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
            since_dt_img = datetime.now(timezone.utc) - timedelta(days=7)
            with sqlite3.connect(DB_PATH) as conn:
                owl_img_snippets = []
                for cid in [int(c) for c in OWL_TOWN_CHAT_IDS]:
                    s = get_weekly_snippets(conn, cid, since_dt_img.isoformat(), limit=10)
                    if s:
                        owl_img_snippets.append(s)
                if owl_img_snippets:
                    owl_image_bytes = generate_weekly_image("\n".join(owl_img_snippets))

        send_to_int = int(OWL_TOWN_SEND_TO)
        if owl_image_bytes:
            await bot.send_photo(chat_id=send_to_int, photo=io.BytesIO(owl_image_bytes))
        await bot.send_message(chat_id=send_to_int, text=owl_text)
        print(f"Sent Owl Town combined report to {send_to_int}")


def main() -> None:
    asyncio.run(send_weekly_async())


if __name__ == "__main__":
    main()
