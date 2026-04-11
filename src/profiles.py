"""
Profile and theme management.

Handles group personality themes, user profiles, case file dossiers,
and dossier milestone announcements.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from config import BOT_PERSONA, GEMINI_API_KEY


# ============================================================
# DB bootstrap
# ============================================================

def ensure_profile_tables(conn: sqlite3.Connection) -> None:
    """Create profile/theme/image/case-note tables if they don't exist.

    Called by both the weekly pipeline and the agent loop at startup.
    Kept here (not in weekly.py) so capability modules don't depend on
    the orchestrator's internals.
    """
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


# ============================================================
# Group themes
# ============================================================

def get_group_theme(conn: sqlite3.Connection, chat_id: int) -> str | None:
    """Retrieve the current group theme text, or None if no profile exists yet."""
    row = conn.execute(
        "SELECT theme_text FROM group_themes WHERE chat_id = ?;",
        (chat_id,),
    ).fetchone()
    return row[0] if row else None


def _get_recent_case_notes(
    conn: sqlite3.Connection,
    chat_id: int,
    target_username: str | None = None,
    limit: int = 10,
) -> str:
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


def update_group_theme(
    conn: sqlite3.Connection, chat_id: int, snippets: str
) -> str:
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


# ============================================================
# User profiles
# ============================================================

def get_user_profile(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Retrieve the current user profile text, or None if no profile exists yet."""
    row = conn.execute(
        "SELECT profile_text FROM user_profiles WHERE user_id = ?;",
        (user_id,),
    ).fetchone()
    return row[0] if row else None


def get_user_snippets(
    conn: sqlite3.Connection,
    chat_id: int,
    display_name: str,
    since_iso: str,
    limit: int = 20,
) -> str:
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


def update_user_profile(
    conn: sqlite3.Connection, user_id: int, username: str, snippets: str
) -> str:
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


# ============================================================
# Case files
# ============================================================

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
                "Reformat this personality profile into a hard-boiled detective case file / dossier. "
                "Use these sections: SUBJECT, STATUS, CONFIDENCE LEVEL, BEHAVIORAL PATTERNS, "
                "KNOWN INTERESTS, COMMUNICATION STYLE, ANALYST NOTES.\n\n"
                f"Confidence level: {confidence} (based on {version} week(s) of observation)\n\n"
                "Keep it under 250 words. Be wry and observational, not mean. "
                "The humor comes from the gap between your analytical tone and the messy "
                "humanity of the subject. End with a brief analyst note that reflects on "
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


# ============================================================
# Dossier milestones
# ============================================================

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
                "A subject's dossier has reached a milestone:\n"
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

    return f"📋 @{username} — Profile Milestone\n\n{milestone_text}"
