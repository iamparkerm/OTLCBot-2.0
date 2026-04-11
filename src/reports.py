"""
Conversation data fetching, report building, recap generation,
gazette writing, and image generation.
"""

import sqlite3
import time
from datetime import datetime, timedelta, timezone

from config import (
    BOT_PERSONA,
    DB_PATH,
    ENABLE_AI_SUMMARY,
    GEMINI_API_KEY,
    OWL_TOWN_CHAT_IDS,
    OWL_TOWN_NAMES,
    OWL_TOWN_SEND_TO,
)
from profiles import get_group_theme


# ============================================================
# Conversation window helpers
# ============================================================

def get_weekly_snippets(
    conn: sqlite3.Connection, chat_id: int, since_iso: str, limit: int = 50
) -> str:
    """Random message samples for a single chat (fallback for low-volume weeks)."""
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
        prev_time = bursts[-1][-1][3]
        curr_time = row[3]
        try:
            gap = (
                datetime.fromisoformat(curr_time) - datetime.fromisoformat(prev_time)
            ).total_seconds()
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

    mid_to_user = {r[0]: r[1] for r in rows if r[0]}
    bursts = _detect_bursts(rows)
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
        all_snippets = []
        for cid in chat_ids:
            s = get_weekly_snippets(conn, cid, since_iso, limit=10)
            if s:
                all_snippets.append(s)
        return "\n".join(all_snippets)

    mid_to_user = {r[0]: r[1] for r in rows if r[0]}
    chat_names = chat_names or {}
    chat_id_map = {r[0]: r[5] for r in rows}
    burst_rows = [(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    bursts = _detect_bursts(burst_rows)
    bursts = [b for b in bursts if len(b) >= min_burst_size]
    bursts.sort(key=len, reverse=True)

    result_parts = []
    char_count = 0
    for burst in bursts:
        burst_chat_ids = [chat_id_map.get(msg[0]) for msg in burst]
        most_common_cid = (
            max(set(burst_chat_ids), key=burst_chat_ids.count) if burst_chat_ids else None
        )
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


# ============================================================
# Grounding block
# ============================================================

def build_grounding_block(
    conn: sqlite3.Connection, chat_id: int | list[int], max_chars: int = 800
) -> str:
    """Assemble a compact context block of facts from the DB for prompt grounding.

    Pulls group theme, open bets, recent watchlist adds, and active user profile
    headlines. Accepts a single chat_id or a list (for Owl Town multi-chat).
    """
    chat_ids = [chat_id] if isinstance(chat_id, int) else chat_id
    placeholders = ",".join("?" * len(chat_ids))
    parts = []

    themes = []
    for cid in chat_ids:
        t = get_group_theme(conn, cid)
        if t:
            themes.append(t[:200])
    if themes:
        parts.append("Personality: " + " | ".join(themes))

    bets = conn.execute(
        f"""SELECT id, description, wager, created_by_name
           FROM bets WHERE chat_id IN ({placeholders}) AND settled_at IS NULL
           ORDER BY created_at LIMIT 5;""",
        tuple(chat_ids),
    ).fetchall()
    if bets:
        bet_strs = [f'#{b[0]} "{b[1]}" ({b[2]}, by @{b[3]})' for b in bets]
        parts.append("Open bets: " + ", ".join(bet_strs))

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


# ============================================================
# AI recap and image generation
# ============================================================

def generate_ai_recap(snippets: str, grounding: str = "") -> str:
    """Call Gemini to produce a 3-4 sentence field report."""
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


def generate_weekly_image(
    snippets: str, context: str = "", retries: int = 2
) -> tuple[bytes, str] | tuple[None, None]:
    """Generate a weekly illustration from conversation snippets.

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
            "ABSOLUTE RULES: zero speech bubbles, zero thought bubbles, zero clouds or wisps "
            "or smoke or vapor or floating shapes of any kind near or above any character's head, "
            "zero text of any kind inside the image, zero labels, zero caption below. "
            "The entire joke must be told through the visual scene alone — expressions, "
            "body language, and what the characters are doing."
        )
        print(f"  Scene summary: {scene}")

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
                        return part.inline_data.data, image_prompt
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


# ============================================================
# Report builders
# ============================================================

def build_weekly_report(chat_id: int) -> str:
    """Build a standalone weekly report for a single chat (Penetr8in)."""
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
    """Build a combined stats block across all Owl Town groups.

    NOTE: No AI recap here — build_weekly_gazette() sends the narrative
    field notes as a separate message before this stats block.
    """
    since_dt = datetime.now(timezone.utc) - timedelta(days=7)
    since = since_dt.isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        chat_ids_int = [int(cid) for cid in OWL_TOWN_CHAT_IDS]
        placeholders = ",".join("?" * len(chat_ids_int))

        grand_total = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE chat_id IN ({placeholders}) AND sent_at_utc >= ?;",
            (*chat_ids_int, since),
        ).fetchone()[0]

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

    return "\n".join(lines)


def build_weekly_gazette(stats_text: str, sincerity_text: str = "") -> str | None:
    """Generate a prose 'weekly gazette' from the detective using Gemini.

    Takes the assembled stats/sincerity block plus recent case notes and
    produces a ~200-word briefing memo sent before the stats block.
    """
    if not GEMINI_API_KEY:
        return None

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
        pass

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
