"""
OTLCBot Observer — runs every 2 hours, writes internal case notes without posting.

Observes each configured chat, files observations to the case_notes table, and
updates group themes when there is enough new material. Never sends any Telegram
messages — it has no bot object and no output beyond the database.

The Observer is one half of the two-agent architecture:
  - Observer (this file)  — reads messages, writes case_notes, runs every 2 hours
  - Speaker (agent.py)    — reads case_notes, decides whether to post, runs every 3 hours

Usage (cron, every 2 hours):
    0 */2 * * * /home/parker/OTLCBot-2.0/.venv/bin/python \\
        /home/parker/OTLCBot-2.0/src/observer.py >> observer.log 2>&1
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=ROOT / ".env")

from config import (
    BOT_PERSONA,
    CHAT_IDS,
    DB_PATH,
    GEMINI_API_KEY,
    OWL_TOWN_CHAT_IDS,
)
from profiles import ensure_profile_tables, get_group_theme, update_group_theme


# ============================================================
# Constants
# ============================================================

OBSERVE_WINDOW_HOURS = 4      # look back this far each run (matches cron interval)
MIN_MESSAGES_TO_OBSERVE = 3   # skip chats quieter than this
OBSERVE_COOLDOWN_HOURS = 48   # don't re-observe a chat more often than this
THEME_UPDATE_THRESHOLD = 20   # update group theme after this many new messages


# ============================================================
# Observation logic
# ============================================================

def _get_recent_messages(
    conn: sqlite3.Connection,
    chat_id: int,
    hours: int = OBSERVE_WINDOW_HOURS,
) -> list[tuple[str, str]]:
    """Return (who, text) pairs from the last N hours, oldest first."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return conn.execute(
        """
        SELECT COALESCE(username, full_name, 'unknown') AS who, text
        FROM messages
        WHERE chat_id = ?
          AND text IS NOT NULL
          AND LENGTH(TRIM(text)) >= 5
          AND sent_at_utc >= ?
        ORDER BY sent_at_utc ASC;
        """,
        (chat_id, since),
    ).fetchall()


def _observe_chat(conn: sqlite3.Connection, chat_id: int) -> None:
    """
    Generate internal observations for one chat and write them to case_notes.
    Optionally updates the group theme if there is enough new material.
    """
    # Skip if this chat was observed within the cooldown window
    last_note = conn.execute(
        "SELECT created_at FROM case_notes WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1;",
        (chat_id,),
    ).fetchone()
    if last_note:
        try:
            last_dt = datetime.fromisoformat(last_note[0])
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since < OBSERVE_COOLDOWN_HOURS:
                print(f"  Observer chat {chat_id}: last observed {hours_since:.1f}h ago — skipping (< {OBSERVE_COOLDOWN_HOURS}h)")
                return
        except (ValueError, TypeError):
            pass

    messages = _get_recent_messages(conn, chat_id)

    if len(messages) < MIN_MESSAGES_TO_OBSERVE:
        print(f"  Observer chat {chat_id}: {len(messages)} messages — skipping (too quiet)")
        return

    snippets = "\n".join(f"{who}: {text[:200]}" for who, text in messages)
    theme = get_group_theme(conn, chat_id) or "(no theme yet)"

    print(f"  Observer chat {chat_id}: {len(messages)} messages to observe")

    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                f"{BOT_PERSONA}\n\n"
                "You are filing internal case notes after reviewing a group chat's recent activity. "
                "These notes are NEVER shown to the group — they go into the private case files "
                "and will inform the bot's future reasoning.\n\n"
                "Generate 1-3 observations. Each should be one of:\n"
                "  - A pattern or dynamic worth tracking (note_type: 'observation')\n"
                "  - A notable discovery about a specific person (note_type: 'discovery')\n\n"
                "Only file notes for things that genuinely stand out. "
                "If the conversation is routine or low-signal, return 0-1 notes. "
                "Prefer quality over quantity.\n\n"
                "Respond with ONLY a valid JSON array:\n"
                '[{"note_type": "observation" | "discovery", '
                '"target_username": "<@username or null for group-level>", '
                '"note": "<1-2 sentence observation in your detective voice>"}]\n\n'
                "Return [] if nothing stands out.\n\n"
                f"Current group personality: {theme}\n\n"
                f"Recent messages ({len(messages)} total, last {OBSERVE_WINDOW_HOURS}h):\n"
                f"{snippets}"
            ),
            config={"max_output_tokens": 300, "temperature": 1.1},
        )

        raw = (response.text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        notes = json.loads(raw)
        if not isinstance(notes, list):
            print(f"  Observer chat {chat_id}: unexpected response format, skipping")
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        filed = 0
        for item in notes:
            note_text = (item.get("note") or "").strip()
            note_type = item.get("note_type", "observation")
            target = item.get("target_username") or None
            if not note_text:
                continue
            conn.execute(
                "INSERT INTO case_notes "
                "(chat_id, note_type, target_username, note_text, created_at) "
                "VALUES (?, ?, ?, ?, ?);",
                (chat_id, note_type, target, note_text, now_iso),
            )
            target_str = f" re: @{target}" if target else ""
            print(f"    Filed [{note_type}{target_str}]: {note_text[:80]}")
            filed += 1

        conn.commit()
        print(f"  Observer chat {chat_id}: filed {filed} note(s)")

        # Update group theme when there is enough fresh conversation
        if len(messages) >= THEME_UPDATE_THRESHOLD:
            print(f"  Observer chat {chat_id}: updating group theme ({len(messages)} new messages)")
            update_group_theme(conn, chat_id, snippets)

    except Exception as e:
        print(f"  Observer chat {chat_id}: failed — {e}")


# ============================================================
# Entry point
# ============================================================

def run_observer(chat_ids: list[int]) -> None:
    """Run the observer across all provided chat IDs.

    Rebuilds the wiki only if at least one new case note was filed — no point
    writing pages when nothing changed.
    """
    if not GEMINI_API_KEY:
        print("Observer: GEMINI_API_KEY not set, exiting.")
        return

    print(f"Observer: starting — {datetime.now(timezone.utc).isoformat()}")
    print(f"Observer: {len(chat_ids)} chat(s) to observe")

    total_filed = 0
    with sqlite3.connect(DB_PATH) as conn:
        ensure_profile_tables(conn)
        for chat_id in chat_ids:
            before = conn.execute(
                "SELECT COUNT(*) FROM case_notes WHERE chat_id = ?;", (chat_id,)
            ).fetchone()[0]
            _observe_chat(conn, chat_id)
            after = conn.execute(
                "SELECT COUNT(*) FROM case_notes WHERE chat_id = ?;", (chat_id,)
            ).fetchone()[0]
            total_filed += after - before

    if total_filed > 0:
        # Rebuild wiki from DB so People, Timeline, and Sincerity pages reflect
        # new observations immediately. Gemini-compiled sections (Topics, channel
        # articles) are skipped here — those update only on the weekly Friday run.
        print(f"Observer: {total_filed} note(s) filed — rebuilding wiki (no-gemini)...")
        try:
            import sys as _sys
            _sys.path.insert(0, str(ROOT / "src"))
            from wiki import build_wiki
            pages = build_wiki(gemini_enabled=False)
            print(f"Observer: wiki rebuilt — {pages} pages written")
        except Exception as e:
            print(f"Observer: wiki rebuild failed — {e}")
    else:
        print("Observer: no new notes filed — skipping wiki rebuild")

    print("Observer: done.\n")


if __name__ == "__main__":
    # All chats: standalone (Penetr8in) + all 6 OT channels
    all_chat_ids: list[int] = [int(cid) for cid in CHAT_IDS]
    for cid in OWL_TOWN_CHAT_IDS:
        cid_int = int(cid)
        if cid_int not in all_chat_ids:
            all_chat_ids.append(cid_int)

    run_observer(all_chat_ids)
