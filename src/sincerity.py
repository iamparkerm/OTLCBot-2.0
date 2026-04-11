"""
DFW Sincerity Index pipeline.

Handles all sincerity/irony scoring: data fetching, Gemini analysis,
DB persistence, trend tracking, and message building.
"""

import sqlite3
from datetime import datetime, timezone

from config import GEMINI_API_KEY


# ============================================================
# Data fetching
# ============================================================

def get_sincerity_snippets(
    conn: sqlite3.Connection, chat_id: int, since_iso: str, limit: int = 50
) -> str:
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


# ============================================================
# Scoring helpers
# ============================================================

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


# ============================================================
# DB reads (trend history)
# ============================================================

def _get_last_week_group_grade(
    conn: sqlite3.Connection, chat_id: int, current_week: str
) -> str | None:
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


def _get_last_week_user_score(
    conn: sqlite3.Connection, chat_id: int, username: str, current_week: str
) -> tuple[str | None, float | None]:
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


# ============================================================
# Gemini analysis
# ============================================================

def analyze_sincerity(snippets: str) -> dict | None:
    """Use Gemini to score irony/sincerity. Returns raw data dict or None."""
    try:
        import json
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                "You are a hard-boiled AI detective running forensic analysis on intercepted "
                "communications, inspired by David Foster Wallace's critique of irony in contemporary "
                "culture. Score the level of irony vs sincerity in these messages.\n\n"
                "For each unique user, estimate what percentage of their messages are ironic "
                "(sarcasm, cynicism, detached humor, performative disinterest, mocking tone) "
                "vs sincere (genuine, earnest, vulnerable, direct, emotionally honest).\n\n"
                "Respond ONLY with valid JSON in this exact format, no other text:\n"
                '{"group_irony_pct": <number 0-100>, "users": {"username1": <number 0-100>, "username2": <number 0-100>}}\n\n'
                "Where the numbers represent the percentage of irony detected (0 = fully sincere, "
                "100 = fully ironic).\n\n"
                f"Messages:\n{snippets}"
            ),
            config={"max_output_tokens": 300, "temperature": 0.7},
        )
        raw = response.text.strip() if response.text else ""
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as e:
        print(f"Sincerity analysis failed: {e}")
        return None


# ============================================================
# DB writes
# ============================================================

def save_sincerity_scores(
    conn: sqlite3.Connection, chat_id: int, week_of: str, data: dict
) -> None:
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


# ============================================================
# Message builders
# ============================================================

def build_group_sincerity_message(
    conn: sqlite3.Connection, chat_id: int, data: dict, week_of: str
) -> str:
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
        "📖 DFW Sincerity Index",
        f"   Irony detected: {irony_int}%",
        trend_str,
        f"   Assessment: {grade}",
    ]
    return "\n".join(lines)


def build_user_dm(
    conn: sqlite3.Connection, chat_id: int, username: str, irony_pct: float, week_of: str
) -> str:
    """Build a private DM for an individual user with their score + trend."""
    grade = _irony_pct_to_grade(irony_pct)
    irony_int = round(irony_pct)
    prev_grade, prev_irony = _get_last_week_user_score(conn, chat_id, username, week_of)
    trend = _trend_arrow(irony_pct, prev_irony)

    lines = [
        f"📖 @{username} — DFW Sincerity Index",
        f"   Irony detected: {irony_int}%",
        f"   Assessment: {grade}",
    ]
    if prev_grade:
        lines.append(f"   Last week: {prev_grade} → This week: {grade}. {trend}")
    else:
        lines.append(f"   {trend}")
    return "\n".join(lines)
