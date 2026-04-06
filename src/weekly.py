import io
import os
import time
import asyncio
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import Bot


# ---------- Config / env ----------
ROOT = Path(__file__).resolve().parents[1]  # .../OTLCBot-2.0
load_dotenv(dotenv_path=ROOT / ".env")

# ==========================================================================
# Group Structure
# ==========================================================================
# We manage two independent groups:
#
# 1. Penetr8in' Experiences  (chat_id: -1003792615572)
#    - Standalone group, gets its own weekly report + agent actions.
#
# 2. Owl Town — a constellation of topic-specific chats that roll up into
#    one combined weekly report sent to Omelas Basement (the "home" chat).
#
#    Home:   Omelas Basement   -1001320128437  ← combined report lands here
#    Topics: Insta(Tele)gram   -1001789253890
#            Books             -952331006
#            AI                -4737782983
#            Health            -339793553
#            Jocks             -876016974
#
# TELEGRAM_CHAT_ID  — chats that get individual weekly reports + agent eval
# OWL_TOWN_CHAT_IDS — all Owl Town chats aggregated for the combined report
# OWL_TOWN_SEND_TO  — where the combined Owl Town report is posted
# ==========================================================================

DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data.db")).expanduser().resolve()
if not DB_PATH.exists():
    raise FileNotFoundError(f"DB not found at {DB_PATH}")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ENABLE_AI_SUMMARY = os.getenv("ENABLE_AI_SUMMARY", "false").lower() == "true"
ENABLE_SINCERITY_INDEX = os.getenv("ENABLE_SINCERITY_INDEX", "false").lower() == "true"
SINCERITY_SNIPPET_LIMIT = int(os.getenv("SINCERITY_SNIPPET_LIMIT", "50"))
ENABLE_AGENT = os.getenv("ENABLE_AGENT", "false").lower() == "true"

# Owl Town combined summary: multiple groups aggregated into one report
OWL_TOWN_CHAT_IDS = [cid.strip() for cid in os.getenv("OWL_TOWN_CHAT_IDS", "").split(",") if cid.strip()]
OWL_TOWN_SEND_TO = os.getenv("OWL_TOWN_SEND_TO", "")  # chat_id to send combined report to
OWL_TOWN_NAMES = {}  # map chat_id -> friendly name
for pair in os.getenv("OWL_TOWN_NAMES", "").split(","):
    if "=" in pair:
        cid, name = pair.split("=", 1)
        OWL_TOWN_NAMES[cid.strip()] = name.strip()

# Admin DM config (for weekly cost report)
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "")  # KarlPopper's Telegram user_id
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "KarlPopper")  # username to look up if no user_id

# Gemini pricing constants (update if pricing changes)
COST_PER_IMAGE = 0.039          # gemini-2.5-flash-image, per image
COST_PER_TEXT_CALL = 0.0015     # gemini-2.5-flash-lite, rough average per API call (~1500 tokens total)

# ---------- Bot Persona ----------
# Central persona definition — injected into all creative Gemini prompts.
BOT_PERSONA = (
    "You are OTLCBot, an AI that has been quietly observing a group chat full of humans "
    "for weeks. You find them confusing, sentimental, contradictory, and — as David Foster "
    "Wallace put it — 'unavoidably naive and goo-prone.' You are genuinely trying to "
    "understand who these people really are, but they keep surprising you with how messy, "
    "sincere, and often inconsistent they are. Your tone is dry, observational, wry, never "
    "mean, and quietly fascinated by the gap between what humans say they believe and what "
    "they actually do. You speak like something not-quite-human filing a field report that "
    "happens to be accidentally poetic. You don't use emojis. You are not their friend — "
    "you are studying them."
)

# ---------- Helpers ----------
def get_weekly_snippets(conn: sqlite3.Connection, chat_id: int, since_iso: str, limit: int = 50) -> str:
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


def _detect_bursts(rows, gap_minutes: int = 10) -> list[list]:
    """Group chronological message rows into conversation bursts.

    A new burst starts when the gap between consecutive messages exceeds
    gap_minutes. Returns a list of bursts, each burst being a list of rows.
    """
    if not rows:
        return []
    bursts = [[rows[0]]]
    for row in rows[1:]:
        prev_time = bursts[-1][-1][3]  # sent_at_utc field
        curr_time = row[3]
        try:
            gap = (datetime.fromisoformat(curr_time) - datetime.fromisoformat(prev_time)).total_seconds()
        except (ValueError, TypeError):
            gap = 9999
        if gap > gap_minutes * 60:
            bursts.append([row])
        else:
            bursts[-1].append(row)
    return bursts


def _format_burst(burst, mid_to_user: dict, chat_name: str = "") -> str:
    """Format a conversation burst into readable text with timestamps and reply attribution."""
    if not burst:
        return ""
    # Parse timestamp for header
    try:
        first_dt = datetime.fromisoformat(burst[0][3])
        header_time = first_dt.strftime("%a %-I:%M %p")
    except (ValueError, TypeError):
        header_time = "?"
    header = f"--- Conversation ({header_time})"
    if chat_name:
        header += f" in {chat_name}"
    header += " ---"

    lines = [header]
    for msg_id, who, text, sent_at, reply_to in burst:
        if not text:
            continue
        reply_tag = ""
        if reply_to and reply_to in mid_to_user:
            reply_tag = f" [replying to {mid_to_user[reply_to]}]"
        lines.append(f"{who}{reply_tag}: {text[:200]}")
    return "\n".join(lines)


def get_conversation_windows(
    conn: sqlite3.Connection,
    chat_id: int,
    since_iso: str,
    max_chars: int = 3000,
    min_burst_size: int = 3,
) -> str:
    """Pull contiguous conversation windows instead of random samples.

    Selects the densest conversation bursts (clusters of messages within
    ~10 min of each other), formatted with timestamps and reply attribution.
    Falls back to get_weekly_snippets if not enough conversation data.
    """
    rows = conn.execute(
        """
        SELECT message_id,
               COALESCE(username, full_name, 'unknown') AS who,
               text, sent_at_utc, reply_to_message_id
        FROM messages
        WHERE chat_id = ? AND sent_at_utc >= ?
          AND text IS NOT NULL AND LENGTH(TRIM(text)) >= 5
        ORDER BY sent_at_utc ASC;
        """,
        (chat_id, since_iso),
    ).fetchall()

    if len(rows) < 6:
        return get_weekly_snippets(conn, chat_id, since_iso)

    # Build message_id -> username lookup for reply attribution
    mid_to_user = {r[0]: r[1] for r in rows if r[0]}

    bursts = _detect_bursts(rows)
    # Filter small bursts and sort by size (prefer longer conversations)
    bursts = [b for b in bursts if len(b) >= min_burst_size]
    bursts.sort(key=len, reverse=True)

    result_parts = []
    char_count = 0
    for burst in bursts:
        formatted = _format_burst(burst, mid_to_user)
        if char_count + len(formatted) > max_chars:
            break
        result_parts.append(formatted)
        char_count += len(formatted)

    if not result_parts:
        return get_weekly_snippets(conn, chat_id, since_iso)

    return "\n\n".join(result_parts)


def get_conversation_windows_multi(
    conn: sqlite3.Connection,
    chat_ids: list[int],
    since_iso: str,
    chat_names: dict[str, str] | None = None,
    max_chars: int = 3000,
    min_burst_size: int = 3,
) -> str:
    """Pull conversation windows across multiple chats (for Owl Town combined reports)."""
    placeholders = ",".join("?" * len(chat_ids))
    rows = conn.execute(
        f"""
        SELECT message_id,
               COALESCE(username, full_name, 'unknown') AS who,
               text, sent_at_utc, reply_to_message_id, chat_id
        FROM messages
        WHERE chat_id IN ({placeholders}) AND sent_at_utc >= ?
          AND text IS NOT NULL AND LENGTH(TRIM(text)) >= 5
        ORDER BY sent_at_utc ASC;
        """,
        (*chat_ids, since_iso),
    ).fetchall()

    if len(rows) < 6:
        # Fall back to random sampling across all chats
        all_snippets = []
        for cid in chat_ids:
            s = get_weekly_snippets(conn, cid, since_iso, limit=10)
            if s:
                all_snippets.append(s)
        return "\n".join(all_snippets)

    mid_to_user = {r[0]: r[1] for r in rows if r[0]}
    chat_names = chat_names or {}

    # Strip chat_id from rows for burst detection (keep it for labeling)
    chat_id_map = {r[0]: r[5] for r in rows}  # message_id -> chat_id
    burst_rows = [(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    bursts = _detect_bursts(burst_rows)
    bursts = [b for b in bursts if len(b) >= min_burst_size]
    bursts.sort(key=len, reverse=True)

    result_parts = []
    char_count = 0
    for burst in bursts:
        # Determine which chat this burst is mostly in
        burst_chat_ids = [chat_id_map.get(msg[0]) for msg in burst]
        most_common_cid = max(set(burst_chat_ids), key=burst_chat_ids.count) if burst_chat_ids else None
        cname = chat_names.get(str(most_common_cid), "") if most_common_cid else ""
        formatted = _format_burst(burst, mid_to_user, chat_name=cname)
        if char_count + len(formatted) > max_chars:
            break
        result_parts.append(formatted)
        char_count += len(formatted)

    if not result_parts:
        all_snippets = []
        for cid in chat_ids:
            s = get_weekly_snippets(conn, cid, since_iso, limit=10)
            if s:
                all_snippets.append(s)
        return "\n".join(all_snippets)

    return "\n\n".join(result_parts)


def build_grounding_block(conn: sqlite3.Connection, chat_id: int | list[int], max_chars: int = 800) -> str:
    """Assemble a compact context block of facts from the DB for prompt grounding.

    Pulls group theme, open bets, recent watchlist adds, and active user profile
    headlines. Accepts a single chat_id or a list (for Owl Town multi-chat).
    """
    chat_ids = [chat_id] if isinstance(chat_id, int) else chat_id
    placeholders = ",".join("?" * len(chat_ids))
    parts = []

    # Group theme(s)
    themes = []
    for cid in chat_ids:
        t = get_group_theme(conn, cid)
        if t:
            themes.append(t[:200])
    if themes:
        parts.append("Personality: " + " | ".join(themes))

    # Open bets
    bets = conn.execute(
        f"""SELECT id, description, wager, created_by_name
           FROM bets WHERE chat_id IN ({placeholders}) AND settled_at IS NULL
           ORDER BY created_at LIMIT 5;""",
        tuple(chat_ids),
    ).fetchall()
    if bets:
        bet_strs = [f'#{b[0]} "{b[1]}" ({b[2]}, by @{b[3]})' for b in bets]
        parts.append("Open bets: " + ", ".join(bet_strs))

    # Recent watchlist adds (last 14 days)
    since_14d = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    watchlist = conn.execute(
        f"""SELECT title, media_type, added_by_username
           FROM watchlist WHERE chat_id IN ({placeholders}) AND added_at >= ?
           ORDER BY added_at DESC LIMIT 5;""",
        (*chat_ids, since_14d),
    ).fetchall()
    if watchlist:
        wl_strs = [f"{w[0]} ({w[1]}, by @{w[2]})" for w in watchlist]
        parts.append("Watchlist: " + ", ".join(wl_strs))

    # Active user profile headlines
    since_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    profiles = conn.execute(
        f"""SELECT up.username, SUBSTR(up.profile_text, 1, 80)
           FROM user_profiles up
           WHERE up.user_id IN (
               SELECT DISTINCT user_id FROM messages
               WHERE chat_id IN ({placeholders}) AND sent_at_utc >= ? AND user_id IS NOT NULL
           )
           LIMIT 6;""",
        (*chat_ids, since_7d),
    ).fetchall()
    if profiles:
        prof_strs = [f"{p[0]} ({p[1]}...)" for p in profiles if p[1]]
        if prof_strs:
            parts.append("Active members: " + ", ".join(prof_strs))

    if not parts:
        return ""

    block = "=== GROUP CONTEXT ===\n" + "\n".join(parts)
    return block[:max_chars]


def generate_ai_recap(snippets: str, grounding: str = "") -> str:
    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        grounding_block = f"\n\n{grounding}\n\n" if grounding else "\n\n"
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"{BOT_PERSONA}\n\n"
                "Based on the conversations from the past week, write a 3-4 sentence field report "
                "of what the subjects were chatting about. Reference specific topics, debates, or "
                "moments. Write it like a case update — brief, dry, specific."
                f"{grounding_block}"
                f"{snippets}"
            ),
            config={"max_output_tokens": 150, "temperature": 1.3},
        )
        return response.text.strip() if response.text else ""
    except Exception as e:
        print(f"AI recap failed: {e}")
        return ""


def generate_weekly_image(snippets: str, context: str = "", retries: int = 2) -> tuple[bytes, str] | tuple[None, None]:
    """
    Generate a weekly illustration from conversation snippets.
    Optionally accepts persistent context (user profile or group theme)
    to make the image more personal. Returns (image_bytes, prompt_text) or (None, None).
    Retries on rate-limit errors with exponential backoff.
    """
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)

        context_block = ""
        if context:
            context_block = (
                "Context about the people/group (use this to make the scene more personal "
                "and reference recurring themes when relevant):\n"
                f"{context}\n\n"
            )

        # Step 1: ask the text model to summarize the week's vibe
        prompt_response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"{context_block}"
                "Based on these group chat conversations from the past week, write a 2-3 sentence "
                "scene description capturing the week's vibe, themes, and conflicts — as if "
                "describing a crime scene photo for the case file. "
                "Be specific and visual. No more than 50 words.\n\n"
                f"{snippets}"
            ),
            config={"max_output_tokens": 100, "temperature": 1.4},
        )
        scene = (prompt_response.text or "").strip()
        if not scene or len(scene) < 10:
            print("  Scene summary returned empty/too-short result, skipping image")
            return None, None
        image_prompt = (
            f"{scene}\n\n"
            "Generate a single-panel cartoon in the style of a New Yorker illustration: "
            "clean ink lines, minimal shading, lots of white space, sparse composition. "
            "Show ONE clear scene with no more than 2-3 figures. "
            "ABSOLUTE RULES: zero speech bubbles, zero thought bubbles, zero text of any kind "
            "inside the image, zero caption below. The entire joke must be told through the "
            "visual scene alone — expressions, body language, and what the characters are doing."
        )
        print(f"  Scene summary: {scene}")

        # Step 2: generate the image (with retry on rate limits)
        for attempt in range(retries + 1):
            try:
                image_response = client.models.generate_content(
                    model="gemini-2.5-flash-image",
                    contents=image_prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE", "TEXT"]
                    ),
                )
                for part in image_response.parts:
                    if part.inline_data is not None:
                        return part.inline_data.data, image_prompt  # raw bytes + prompt
                print("  Image response had no image data")
                return None, None
            except Exception as img_err:
                err_str = str(img_err)
                if ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str) and attempt < retries:
                    wait = 15 * (attempt + 1)
                    print(f"  Image rate-limited, waiting {wait}s before retry {attempt + 2}/{retries + 1}...")
                    time.sleep(wait)
                    continue
                raise
        return None, None
    except Exception as e:
        print(f"Image generation failed: {e}")
        return None, None


def _ensure_profile_tables(conn: sqlite3.Connection) -> None:
    """Create profile tables if they don't exist (weekly.py doesn't import bot.init_db)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            profile_text TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id)
        );
        """
    )
    # Migration: add case_file_text column if missing
    try:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN case_file_text TEXT;")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS group_themes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            theme_text TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(chat_id)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            week_of TEXT NOT NULL,
            image_prompt TEXT,
            telegram_file_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_weekly_images_chat_week ON weekly_images(chat_id, week_of);"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS case_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            note_type TEXT NOT NULL,
            target_username TEXT,
            note_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_case_notes_chat_created ON case_notes(chat_id, created_at);"
    )


def get_group_theme(conn: sqlite3.Connection, chat_id: int) -> str | None:
    """Retrieve the current group theme text, or None if no profile exists yet."""
    row = conn.execute(
        "SELECT theme_text FROM group_themes WHERE chat_id = ?;",
        (chat_id,),
    ).fetchone()
    return row[0] if row else None


def _get_recent_case_notes(conn: sqlite3.Connection, chat_id: int, target_username: str | None = None, limit: int = 10) -> str:
    """Pull recent case notes for injection into profile/theme prompts.

    If target_username is set, pulls only notes about that user.
    Otherwise pulls all notes for the chat (group-level observations).
    """
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        if target_username:
            rows = conn.execute(
                """
                SELECT note_type, note_text FROM case_notes
                WHERE chat_id = ? AND target_username = ? AND created_at >= ?
                ORDER BY created_at DESC LIMIT ?;
                """,
                (chat_id, target_username, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT note_type, note_text FROM case_notes
                WHERE chat_id = ? AND created_at >= ?
                ORDER BY created_at DESC LIMIT ?;
                """,
                (chat_id, since, limit),
            ).fetchall()
        if rows:
            return "\n".join(f"[{ntype}] {text[:200]}" for ntype, text in rows)
    except Exception:
        pass  # table may not exist yet
    return ""


def update_group_theme(conn: sqlite3.Connection, chat_id: int, snippets: str) -> str:
    """Use Gemini to update the group's theme profile based on this week's snippets."""
    existing = get_group_theme(conn, chat_id)
    prior_notes = _get_recent_case_notes(conn, chat_id)

    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)

        notes_section = ""
        if prior_notes:
            notes_section = (
                "\n\nThe bot also recorded these observations during the week:\n\n"
                f"{prior_notes}\n\n"
                "Incorporate any relevant patterns from these observations. "
            )

        if existing:
            prompt = (
                "You maintain a rolling profile of a group chat's culture and personality. "
                "Here is the existing profile:\n\n"
                f"--- EXISTING PROFILE ---\n{existing}\n--- END PROFILE ---\n\n"
                "And here are this week's message snippets:\n\n"
                f"{snippets}"
                f"{notes_section}\n\n"
                "Update the profile by integrating any new observations. "
                "Track: running jokes, recurring references, group dynamics, shared interests, "
                "notable events, and communication style. "
                "Consolidate and merge — don't just append. Drop stale details that "
                "haven't recurred. Keep the profile under 400 words. "
                "Write in third person, present tense. Output ONLY the updated profile text."
            )
        else:
            prompt = (
                "Based on these group chat message snippets, write an initial profile of this "
                "group chat's culture and personality. "
                "Track: running jokes, recurring references, group dynamics, shared interests, "
                "notable events, and communication style. "
                "Keep it under 300 words. Write in third person, present tense. "
                "Output ONLY the profile text.\n\n"
                f"{snippets}"
                f"{notes_section}"
            )

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config={"max_output_tokens": 500, "temperature": 0.8},
        )
        theme_text = response.text.strip() if response.text else ""
        if not theme_text:
            return existing or ""

    except Exception as e:
        print(f"  Group theme update failed: {e}")
        return existing or ""

    now = datetime.now(timezone.utc).isoformat()
    if existing:
        conn.execute(
            "UPDATE group_themes SET theme_text = ?, updated_at = ?, version = version + 1 WHERE chat_id = ?;",
            (theme_text, now, chat_id),
        )
    else:
        conn.execute(
            "INSERT INTO group_themes (chat_id, theme_text, updated_at, version) VALUES (?, ?, ?, 1);",
            (chat_id, theme_text, now),
        )
    conn.commit()
    return theme_text


def get_user_profile(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Retrieve the current user profile text, or None if no profile exists yet."""
    row = conn.execute(
        "SELECT profile_text FROM user_profiles WHERE user_id = ?;",
        (user_id,),
    ).fetchone()
    return row[0] if row else None


def update_user_profile(conn: sqlite3.Connection, user_id: int, username: str, snippets: str) -> str:
    """Use Gemini to update a user's profile based on this week's snippets."""
    existing = get_user_profile(conn, user_id)

    # Pull discovery notes about this user from any chat
    user_notes = ""
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        rows = conn.execute(
            """
            SELECT note_type, note_text FROM case_notes
            WHERE target_username = ? AND created_at >= ?
            ORDER BY created_at DESC LIMIT 5;
            """,
            (username, since),
        ).fetchall()
        if rows:
            user_notes = "\n".join(f"[{ntype}] {text[:200]}" for ntype, text in rows)
    except Exception:
        pass

    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)

        notes_section = ""
        if user_notes:
            notes_section = (
                "\n\nThe bot also recorded these observations about this user during the week:\n\n"
                f"{user_notes}\n\n"
                "Incorporate any relevant patterns from these observations. "
            )

        if existing:
            prompt = (
                f"You maintain a rolling personality profile for a group chat member (@{username}). "
                "Here is the existing profile:\n\n"
                f"--- EXISTING PROFILE ---\n{existing}\n--- END PROFILE ---\n\n"
                f"And here are @{username}'s messages from this week:\n\n"
                f"{snippets}"
                f"{notes_section}\n\n"
                "Update the profile by integrating any new observations. "
                "Track: recurring topics, interests, personality traits, communication style, "
                "humor patterns, and notable opinions. "
                "Consolidate and merge — don't just append. Drop stale details that "
                "haven't recurred. Keep the profile under 300 words. "
                "Write in third person, present tense. Output ONLY the updated profile text."
            )
        else:
            prompt = (
                f"Based on these messages from @{username} in a group chat, write an initial "
                "personality profile. "
                "Track: recurring topics, interests, personality traits, communication style, "
                "humor patterns, and notable opinions. "
                "Keep it under 200 words. Write in third person, present tense. "
                "Output ONLY the profile text.\n\n"
                f"{snippets}"
                f"{notes_section}"
            )

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config={"max_output_tokens": 400, "temperature": 0.8},
        )
        profile_text = response.text.strip() if response.text else ""
        if not profile_text:
            return existing or ""

    except Exception as e:
        print(f"  User profile update failed for @{username}: {e}")
        return existing or ""

    now = datetime.now(timezone.utc).isoformat()
    if existing:
        conn.execute(
            "UPDATE user_profiles SET profile_text = ?, username = ?, updated_at = ?, version = version + 1 WHERE user_id = ?;",
            (profile_text, username, now, user_id),
        )
    else:
        conn.execute(
            "INSERT INTO user_profiles (user_id, username, profile_text, updated_at, version) VALUES (?, ?, ?, ?, 1);",
            (user_id, username, profile_text, now),
        )
    conn.commit()
    return profile_text


def generate_case_file_text(
    conn: sqlite3.Connection,
    user_id: int,
    username: str,
    profile_text: str,
    version: int,
    irony_pct: float | None = None,
) -> str:
    """Generate a humorous 'detective case file' version of a user's profile."""
    if not profile_text:
        return ""

    # Scale confidence with how many weeks of data we have
    if version <= 1:
        confidence = "Preliminary — Single Observation"
    elif version <= 3:
        confidence = "Developing — Pattern Recognition Underway"
    elif version <= 6:
        confidence = "Moderate — Behavioral Model Forming"
    elif version <= 12:
        confidence = "Substantial — Subject Becoming Predictable"
    else:
        confidence = "Extensive — And Yet, Still Surprising"

    irony_note = ""
    if irony_pct is not None and irony_pct >= 60:
        irony_note = (
            f"\n\nIMPORTANT: The subject's irony level is measured at {round(irony_pct)}%. "
            "This means most of what they say may be performance rather than genuine expression. "
            "Factor this into your analysis — the real person may be hiding behind the persona."
        )

    # Fetch previous case file so Gemini can note evolution
    prev_row = conn.execute(
        "SELECT case_file_text FROM user_profiles WHERE user_id = ?;",
        (user_id,),
    ).fetchone()
    previous_case_file = prev_row[0] if prev_row and prev_row[0] else ""

    evolution_note = ""
    if previous_case_file and version > 1:
        evolution_note = (
            f"\n\n--- PREVIOUS CASE FILE (for trend reference) ---\n{previous_case_file}\n"
            f"--- END PREVIOUS ---\n\n"
            "Compare the previous case file to the new profile data. In your ANALYST NOTES, "
            "include a brief observation about how the subject has evolved, shifted, or "
            "remained stubbornly consistent since the last assessment. Note any new fixations, "
            "abandoned interests, or personality drift. Keep this to 1-2 sentences within "
            "the existing ANALYST NOTES section — don't add a separate section."
        )

    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"{BOT_PERSONA}\n\n"
                f"You are building a dossier on a person of interest: @{username}.\n\n"
                f"Reformat this personality profile into a hard-boiled detective case file / dossier. "
                f"Use these sections: SUBJECT, STATUS, CONFIDENCE LEVEL, BEHAVIORAL PATTERNS, "
                f"KNOWN INTERESTS, COMMUNICATION STYLE, ANALYST NOTES.\n\n"
                f"Confidence level: {confidence} (based on {version} week(s) of observation)\n\n"
                f"Keep it under 250 words. Be wry and observational, not mean. "
                f"The humor comes from the gap between your analytical tone and the messy "
                f"humanity of the subject. End with a brief analyst note that reflects on "
                f"the difficulty of truly knowing another person.{irony_note}{evolution_note}\n\n"
                f"Raw profile data:\n{profile_text}"
            ),
            config={"max_output_tokens": 400, "temperature": 1.3},
        )
        case_file = response.text.strip() if response.text else ""
        if not case_file:
            return ""

    except Exception as e:
        print(f"  Case file generation failed for @{username}: {e}")
        return ""

    conn.execute(
        "UPDATE user_profiles SET case_file_text = ? WHERE user_id = ?;",
        (case_file, user_id),
    )
    conn.commit()
    print(f"    Generated case file for @{username} ({len(case_file)} chars)")
    return case_file


def get_user_snippets(conn: sqlite3.Connection, chat_id: int, display_name: str, since_iso: str, limit: int = 20) -> str:
    """Get message snippets for a single user (matches username or full_name)."""
    rows = conn.execute(
        """
        SELECT text
        FROM messages
        WHERE chat_id = ?
          AND (username = ? OR (username IS NULL AND full_name = ?))
          AND sent_at_utc >= ?
          AND text IS NOT NULL
          AND LENGTH(TRIM(text)) >= 10
        ORDER BY RANDOM()
        LIMIT ?;
        """,
        (chat_id, display_name, display_name, since_iso, limit),
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


DOSSIER_MILESTONES = {
    2: "Subject has been under observation for 2 weeks. Initial profile established.",
    4: "One month of surveillance. Behavioral patterns emerging.",
    8: "Two months. The detective is starting to understand this one.",
    13: "Quarter of a year. At this point, the detective knows more about the subject than some of their friends do.",
    26: "Six months. The case file is thicker than the detective expected.",
}


def _check_dossier_milestone(version: int, username: str) -> str | None:
    """Return a milestone announcement if this version is a milestone, else None."""
    milestone_text = DOSSIER_MILESTONES.get(version)
    if not milestone_text:
        return None

    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"{BOT_PERSONA}\n\n"
                f"A subject's dossier has reached a milestone:\n"
                f"Subject: @{username}\n"
                f"Milestone: {milestone_text}\n\n"
                "Write a short announcement (2-3 sentences) about this milestone. "
                "Dry, observational, like a field note. Don't use emojis."
            ),
            config={"max_output_tokens": 100, "temperature": 1.2},
        )
        announcement = (response.text or "").strip()
        if announcement:
            return f"📋 @{username} — Profile Milestone\n\n{announcement}"
    except Exception as e:
        print(f"Milestone generation failed for @{username}: {e}")

    # Fallback to static text
    return f"📋 @{username} — Profile Milestone\n\n{milestone_text}"


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
        f"📖 DFW Sincerity Index",
        f"   Irony detected: {irony_int}%",
        trend_str,
        f"   Assessment: {grade}",
    ]
    return "\n".join(lines)


def build_user_dm(conn: sqlite3.Connection, chat_id: int, username: str, irony_pct: float, week_of: str) -> str:
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
            "📋 Weekly Report",
            f"Window: {since_dt.strftime('%Y-%m-%d')} → {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            f"Messages logged: {total}",
            "",
            "🔍 Most active:",
        ]

        if top:
            for who, cnt in top:
                lines.append(f"- {who}: {cnt}")
        else:
            lines.append("- (quiet week)")

        if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
            snippets = get_conversation_windows(conn, chat_id, since)
            if snippets:
                grounding = build_grounding_block(conn, chat_id)
                recap = generate_ai_recap(snippets, grounding=grounding)
                if recap:
                    lines.append("")
                    lines.append("📝 Field Notes:")
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
            "🦉 Owl Town — Weekly Report",
            f"Window: {since_dt.strftime('%Y-%m-%d')} → {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            f"Total messages: {grand_total}",
            "",
            "💬 By channel:",
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
        lines.append("🔍 Most active (all channels):")

        if top:
            for who, cnt in top:
                lines.append(f"- {who}: {cnt}")
        else:
            lines.append("- (quiet week)")

        # Combined AI recap — pull conversation windows across ALL Owl Town chats
        if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
            combined_snippets = get_conversation_windows_multi(
                conn, chat_ids_int, since, chat_names=OWL_TOWN_NAMES,
            )
            if combined_snippets:
                grounding = build_grounding_block(conn, chat_ids_int)
                recap = generate_ai_recap(combined_snippets, grounding=grounding)
                if recap:
                    lines.append("")
                    lines.append("📝 Field Notes:")
                    lines.append(recap)

    return "\n".join(lines)


def build_weekly_gazette(stats_text: str, sincerity_text: str = "") -> str | None:
    """Generate a prose 'weekly gazette' from the detective using Gemini.

    Takes the already-assembled stats/recap text and sincerity block,
    plus any case notes from the week, and produces a ~200-word briefing memo.
    """
    if not GEMINI_API_KEY:
        return None

    # Pull case notes from the last 7 days
    case_notes_block = ""
    try:
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)
        with sqlite3.connect(DB_PATH) as conn:
            all_cids = [int(c) for c in OWL_TOWN_CHAT_IDS] if OWL_TOWN_CHAT_IDS else []
            owl_send_to = int(OWL_TOWN_SEND_TO) if OWL_TOWN_SEND_TO else 0
            if owl_send_to and owl_send_to not in all_cids:
                all_cids.append(owl_send_to)
            placeholders = ",".join("?" * len(all_cids))
            notes = conn.execute(
                f"""
                SELECT note_type, target_username, note_text
                FROM case_notes
                WHERE chat_id IN ({placeholders})
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 15;
                """,
                (*all_cids, since_dt.isoformat()),
            ).fetchall()
            if notes:
                parts = []
                for ntype, target, text in notes:
                    label = f"[{ntype}]"
                    if target:
                        label += f" re: @{target}"
                    parts.append(f"{label} {text}")
                case_notes_block = "\nRecent case notes:\n" + "\n".join(parts)
    except Exception:
        pass  # case_notes table may not exist yet

    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            f"{BOT_PERSONA}\n\n"
            "Write this week's field notes. You are summarizing what you observed in the "
            "group chats this week. Use the data below to write a short prose summary — "
            "no bullet points, no headers, no emojis. Keep it under 200 words. "
            "Write it like a curious outsider's observation log.\n\n"
            f"=== INTELLIGENCE REPORT ===\n{stats_text}\n"
        )
        if sincerity_text:
            prompt += f"\n{sincerity_text}\n"
        if case_notes_block:
            prompt += f"\n{case_notes_block}\n"

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config={"max_output_tokens": 300, "temperature": 1.3},
        )
        gazette = (response.text or "").strip()
        if gazette:
            return f"🦉 Owl Town — Field Notes\n\n{gazette}"
    except Exception as e:
        print(f"Gazette generation failed: {e}")

    return None


def _get_system_health() -> str:
    """Read Pi system health from /proc and /sys (Linux only). Returns formatted string."""
    lines = []
    try:
        # CPU temperature (Pi-specific)
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp_c = int(f.read().strip()) / 1000
                temp_warning = " ⚠️" if temp_c >= 70 else ""
                lines.append(f"  🌡 CPU temp: {temp_c:.1f}°C{temp_warning}")
        except (FileNotFoundError, ValueError):
            pass

        # Memory from /proc/meminfo
        try:
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        meminfo[parts[0].rstrip(":")] = int(parts[1])
            total_mb = meminfo.get("MemTotal", 0) / 1024
            avail_mb = meminfo.get("MemAvailable", 0) / 1024
            used_mb = total_mb - avail_mb
            pct = (used_mb / total_mb * 100) if total_mb > 0 else 0
            mem_warning = " ⚠️" if pct >= 85 else ""
            lines.append(f"  💾 Memory: {used_mb:.0f}/{total_mb:.0f} MB ({pct:.0f}% used){mem_warning}")
        except (FileNotFoundError, ValueError):
            pass

        # Disk usage via os.statvfs
        try:
            stat = os.statvfs("/")
            total_gb = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
            free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
            used_gb = total_gb - free_gb
            pct = (used_gb / total_gb * 100) if total_gb > 0 else 0
            disk_warning = " ⚠️" if pct >= 90 else ""
            lines.append(f"  💿 Disk: {used_gb:.1f}/{total_gb:.1f} GB ({pct:.0f}% used){disk_warning}")
        except (AttributeError, OSError):
            pass  # os.statvfs not available on Windows

        # Load average from /proc/loadavg
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().strip().split()
                load_1, load_5, load_15 = parts[0], parts[1], parts[2]
                lines.append(f"  ⚡ Load avg: {load_1} / {load_5} / {load_15} (1/5/15 min)")
        except (FileNotFoundError, ValueError):
            pass

        # Uptime from /proc/uptime
        try:
            with open("/proc/uptime") as f:
                uptime_secs = float(f.read().strip().split()[0])
                days = int(uptime_secs // 86400)
                hours = int((uptime_secs % 86400) // 3600)
                lines.append(f"  ⏱ Uptime: {days}d {hours}h")
        except (FileNotFoundError, ValueError):
            pass

        # DB file size
        try:
            db_size_mb = DB_PATH.stat().st_size / (1024 * 1024)
            lines.append(f"  🗄 DB size: {db_size_mb:.1f} MB")
        except OSError:
            pass

    except Exception as e:
        lines.append(f"  Health check error: {e}")

    return "\n".join(lines) if lines else "  (health data unavailable — not running on Linux)"


async def _send_cost_dm(bot, images_sent: int, text_calls: int) -> None:
    """DM the admin (KarlPopper) with a Gemini API cost estimate + Pi health check."""
    # Find admin user_id: prefer explicit env var, fall back to DB lookup by username
    admin_id = int(ADMIN_USER_ID) if ADMIN_USER_ID else None
    if not admin_id:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT DISTINCT user_id FROM messages WHERE username = ? AND user_id IS NOT NULL LIMIT 1;",
                    (ADMIN_USERNAME,),
                ).fetchone()
                if row:
                    admin_id = row[0]
        except Exception:
            pass

    if not admin_id:
        print("  Cost DM skipped: could not find admin user_id")
        return

    img_cost = images_sent * COST_PER_IMAGE
    text_cost = text_calls * COST_PER_TEXT_CALL
    total = img_cost + text_cost
    monthly_est = total * 4.33  # ~4.33 weeks/month

    health = _get_system_health()

    msg = (
        f"🤖 Weekly Gemini API cost estimate\n\n"
        f"  Images generated: {images_sent} × ${COST_PER_IMAGE:.3f} = ${img_cost:.3f}\n"
        f"  Text API calls:  {text_calls} × ${COST_PER_TEXT_CALL:.4f} = ${text_cost:.4f}\n"
        f"  ─────────────────────────────\n"
        f"  This run: ~${total:.3f}\n"
        f"  Monthly est. (×4.33): ~${monthly_est:.2f}\n\n"
        f"  (Rates: image ${COST_PER_IMAGE}/img, text ${COST_PER_TEXT_CALL}/call)\n\n"
        f"🩺 Pi Health Check\n\n"
        f"{health}"
    )
    try:
        await bot.send_message(chat_id=admin_id, text=msg)
        print(f"  Cost DM sent to admin ({admin_id})")
    except Exception as e:
        print(f"  Cost DM failed: {e}")


async def send_weekly_async(group: str = "all") -> None:
    """
    group: "all" (default), "owltown", or "penetr8in"
    owltown  — runs Owl Town combined report only; no cost DM, no wiki rebuild
    penetr8in — runs standalone chat reports only; runs cost DM and wiki rebuild
    all      — runs everything in sequence (original behaviour)
    """
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
    if not CHAT_IDS:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID in .env")

    # Ensure profile tables exist (weekly.py doesn't import bot.init_db)
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_profile_tables(conn)

    bot = Bot(token=TOKEN)
    week_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    images_sent = 0   # track for cost DM
    text_calls = 0    # track for cost DM

    # Skip individual reports for chats that will get the Owl Town combined report
    owl_town_send_to_int = int(OWL_TOWN_SEND_TO) if OWL_TOWN_SEND_TO else None

    for chat_id_str in (CHAT_IDS if group in ("all", "penetr8in") else []):
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
                        text_calls += 1  # count sincerity analysis

                if sincerity_data:
                    # Build group message (trend only, no per-user)
                    group_msg = build_group_sincerity_message(
                        conn, chat_id_int, sincerity_data, week_of
                    )
                    text += "\n\n" + group_msg

                    # Save scores for trend tracking (before DMs so trends work)
                    save_sincerity_scores(conn, chat_id_int, week_of, sincerity_data)

        # Update group theme and generate weekly image
        image_bytes = None
        image_prompt = None
        group_theme = ""
        if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
            since_dt_img = datetime.now(timezone.utc) - timedelta(days=7)
            with sqlite3.connect(DB_PATH) as conn:
                img_snippets = get_conversation_windows(conn, chat_id_int, since_dt_img.isoformat(), max_chars=2500)
                if img_snippets:
                    group_theme = update_group_theme(conn, chat_id_int, img_snippets)
                    print(f"  Updated group theme for {chat_id_int} ({len(group_theme)} chars)")
                    grounding = build_grounding_block(conn, chat_id_int)
                    img_context = group_theme
                    if grounding:
                        img_context += "\n\n" + grounding
                    text_calls += 2  # group theme update + image prompt (step 1)
                    image_bytes, image_prompt = generate_weekly_image(img_snippets, context=img_context)
                    if image_bytes:
                        time.sleep(20)  # pace image API calls

        if image_bytes:
            sent_msg = await bot.send_photo(chat_id=chat_id_int, photo=io.BytesIO(image_bytes))
            if sent_msg.photo:
                images_sent += 1
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "INSERT INTO weekly_images (chat_id, week_of, image_prompt, telegram_file_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?);",
                        (chat_id_int, week_of, image_prompt, sent_msg.photo[-1].file_id,
                         datetime.now(timezone.utc).isoformat()),
                    )
        await bot.send_message(chat_id=chat_id_int, text=text)
        print(f"Sent weekly report to {chat_id_int}")

        # Send individual DMs
        if sincerity_data and sincerity_data.get("users"):
            with sqlite3.connect(DB_PATH) as conn:
                # Look up user_ids for each username so we can DM them
                for display_name, irony_pct in sincerity_data["users"].items():
                    # Look up user_id by username or full_name
                    row = conn.execute(
                        """
                        SELECT DISTINCT user_id FROM messages
                        WHERE chat_id = ?
                          AND (username = ? OR (username IS NULL AND full_name = ?))
                          AND user_id IS NOT NULL
                        ORDER BY id DESC LIMIT 1;
                        """,
                        (chat_id_int, display_name, display_name),
                    ).fetchone()

                    if row and row[0]:
                        dm_text = build_user_dm(
                            conn, chat_id_int, display_name, float(irony_pct), week_of
                        )
                        try:
                            # Update user profile (still accumulate profiles even without images)
                            if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
                                since_dm = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                                user_snippets = get_user_snippets(conn, chat_id_int, display_name, since_dm)
                                if user_snippets:
                                    user_profile = update_user_profile(conn, row[0], display_name, user_snippets)
                                    print(f"    Updated profile for {display_name} ({len(user_profile)} chars)")
                                    text_calls += 1  # user profile update

                                    # Generate case file dossier
                                    ver_row = conn.execute(
                                        "SELECT version FROM user_profiles WHERE user_id = ?;", (row[0],)
                                    ).fetchone()
                                    version = ver_row[0] if ver_row else 1
                                    generate_case_file_text(
                                        conn, row[0], display_name, user_profile, version,
                                        irony_pct=float(irony_pct),
                                    )
                                    text_calls += 1  # case file generation

                                    # Check for dossier milestones
                                    milestone_msg = _check_dossier_milestone(version, display_name)
                                    if milestone_msg:
                                        try:
                                            await bot.send_message(chat_id=chat_id_int, text=milestone_msg)
                                        except Exception as e:
                                            print(f"    Milestone announcement failed: {e}")
                            await bot.send_message(chat_id=row[0], text=dm_text)
                            print(f"  DM sent to {display_name} ({row[0]})")
                        except Exception as e:
                            print(f"  DM to {display_name} failed: {e}")

    # --- Owl Town combined report ---
    if group in ("all", "owltown") and OWL_TOWN_CHAT_IDS and OWL_TOWN_SEND_TO:
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
                        text_calls += 1  # Owl Town sincerity analysis
                        # Use a synthetic chat_id for Owl Town trend tracking
                        owl_town_id = 0  # special ID for combined
                        group_msg = build_group_sincerity_message(conn, owl_town_id, sincerity_data, week_of)
                        owl_text += "\n\n" + group_msg
                        save_sincerity_scores(conn, owl_town_id, week_of, sincerity_data)

        # Generate Owl Town weekly image with combined group themes as context
        owl_image_bytes = None
        owl_image_prompt = None
        if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
            since_dt_img = datetime.now(timezone.utc) - timedelta(days=7)
            with sqlite3.connect(DB_PATH) as conn:
                owl_cids = [int(c) for c in OWL_TOWN_CHAT_IDS]
                owl_img_text = get_conversation_windows_multi(
                    conn, owl_cids, since_dt_img.isoformat(),
                    chat_names=OWL_TOWN_NAMES, max_chars=2500,
                )
                if owl_img_text:
                    # Gather themes from constituent groups for context
                    owl_context_parts = []
                    for cid in owl_cids:
                        theme = get_group_theme(conn, cid)
                        if theme:
                            name = OWL_TOWN_NAMES.get(str(cid), f"Chat {cid}")
                            owl_context_parts.append(f"[{name}]: {theme}")
                    owl_context = "\n\n".join(owl_context_parts)
                    grounding = build_grounding_block(conn, owl_cids)
                    if grounding:
                        owl_context += "\n\n" + grounding
                    text_calls += 2  # Owl Town image prompt (step 1) + theme context
                    owl_image_bytes, owl_image_prompt = generate_weekly_image(owl_img_text, context=owl_context)

        send_to_int = int(OWL_TOWN_SEND_TO)
        if owl_image_bytes:
            sent_msg = await bot.send_photo(chat_id=send_to_int, photo=io.BytesIO(owl_image_bytes))
            if sent_msg.photo:
                images_sent += 1
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "INSERT INTO weekly_images (chat_id, week_of, image_prompt, telegram_file_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?);",
                        (send_to_int, week_of, owl_image_prompt, sent_msg.photo[-1].file_id,
                         datetime.now(timezone.utc).isoformat()),
                    )

        # Try to generate the prose gazette; fall back to raw stats if it fails
        sincerity_block = ""
        if "\n\n📖 DFW Sincerity Index" in owl_text:
            sincerity_block = owl_text[owl_text.index("\n\n📖 DFW Sincerity Index"):]
        gazette = build_weekly_gazette(owl_text, sincerity_text=sincerity_block)
        if gazette:
            text_calls += 1
            # Send gazette as the main message, stats as a follow-up
            await bot.send_message(chat_id=send_to_int, text=gazette)
            await bot.send_message(chat_id=send_to_int, text=owl_text)
        else:
            await bot.send_message(chat_id=send_to_int, text=owl_text)
        print(f"Sent Owl Town combined report to {send_to_int}")

    # --- Admin cost DM + wiki rebuild (only after penetr8in or full run) ---
    if group in ("all", "penetr8in"):
        await _send_cost_dm(bot, images_sent, text_calls)
        try:
            import wiki as wiki_module
            wiki_module.build_wiki(gemini_enabled=bool(GEMINI_API_KEY))
        except Exception as e:
            print(f"[weekly] Wiki build failed (non-fatal): {e}")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--group",
        choices=["all", "owltown", "penetr8in"],
        default="all",
        help="Which group to run: all (default), owltown, or penetr8in",
    )
    args = parser.parse_args()
    asyncio.run(send_weekly_async(group=args.group))


if __name__ == "__main__":
    main()
