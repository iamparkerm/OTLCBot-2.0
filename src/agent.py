"""
OTLCBot Agent — Observe-Reason-Act decision layer with tool registry.

Each tool is a decorated async function that self-registers its name,
description, and guidelines. The system prompt and dispatch are generated
automatically from the registry — adding a tool is just writing one function.

Usage:
    # From weekly cron (Friday 3pm):
    await run_agent_loop(chat_ids, bot, force_weekly=True)

    # From message-count trigger in bot.py:
    await run_agent_loop([chat_id], bot)
"""

import io
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=ROOT / ".env")

DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data.db")).expanduser().resolve()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
AGENT_MSG_THRESHOLD = int(os.getenv("AGENT_MSG_THRESHOLD", "50"))

# Import existing tools from weekly.py
from weekly import (
    get_weekly_snippets,
    generate_ai_recap,
    generate_weekly_image,
    build_weekly_report,
    build_owl_town_report,
    analyze_sincerity,
    save_sincerity_scores,
    build_group_sincerity_message,
    build_user_dm,
    get_sincerity_snippets,
    get_group_theme,
    update_group_theme,
    update_user_profile,
    generate_case_file_text,
    get_user_snippets,
    _ensure_profile_tables,
    _send_cost_dm,
    ENABLE_AI_SUMMARY,
    ENABLE_SINCERITY_INDEX,
    SINCERITY_SNIPPET_LIMIT,
    COST_PER_IMAGE,
    COST_PER_TEXT_CALL,
)


# ============================================================
# Tool Registry
# ============================================================

TOOLS: dict[str, dict] = {}


def register_tool(name: str, description: str, guidelines: str = "", cost: float | None = None):
    """
    Decorator that registers an async function as an agent tool.

    The function must have signature:
        async def tool_fn(conn, chat_id, bot, params) -> bool

    Args:
        name: Action name the LLM will use (e.g. "nudge_bet").
        description: One-line description for the system prompt.
        guidelines: When to use / not use this tool (shown in prompt).
        cost: Optional per-use cost hint shown to the LLM (e.g. 0.04).
    """
    def wrapper(fn):
        TOOLS[name] = {
            "description": description,
            "guidelines": guidelines,
            "cost": cost,
            "execute": fn,
        }
        return fn
    return wrapper


# ============================================================
# DB setup
# ============================================================

def ensure_agent_table(conn: sqlite3.Connection) -> None:
    """Create the agent_actions table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            reason TEXT,
            executed_at TEXT NOT NULL,
            success INTEGER DEFAULT 1
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_actions_chat ON agent_actions(chat_id, executed_at);"
    )


# ============================================================
# Observe
# ============================================================

def gather_context(conn: sqlite3.Connection, chat_id: int) -> dict:
    """Build a snapshot of current group state for the agent to reason about."""
    now = datetime.now(timezone.utc)

    # Message counts at different time windows
    counts = {}
    for label, hours in [("6h", 6), ("24h", 24), ("7d", 168)]:
        since = (now - timedelta(hours=hours)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND sent_at_utc >= ?;",
            (chat_id, since),
        ).fetchone()
        counts[label] = row[0] if row else 0

    # Active users in last 6h
    since_6h = (now - timedelta(hours=6)).isoformat()
    active_users = [
        row[0] for row in conn.execute(
            """
            SELECT DISTINCT COALESCE(username, full_name, 'unknown')
            FROM messages
            WHERE chat_id = ? AND sent_at_utc >= ?;
            """,
            (chat_id, since_6h),
        ).fetchall()
    ]

    # Recent message snippets (last 15, chronological)
    recent = conn.execute(
        """
        SELECT COALESCE(username, full_name, 'unknown') AS who, text
        FROM messages
        WHERE chat_id = ? AND text IS NOT NULL AND LENGTH(TRIM(text)) >= 5
        ORDER BY sent_at_utc DESC
        LIMIT 15;
        """,
        (chat_id,),
    ).fetchall()
    recent_snippets = [f"{who}: {text[:150]}" for who, text in reversed(recent) if text]

    # Open bets and their age
    open_bets = conn.execute(
        """
        SELECT id, description, created_at
        FROM bets
        WHERE chat_id = ? AND settled_at IS NULL
        ORDER BY created_at;
        """,
        (chat_id,),
    ).fetchall()
    bets_info = []
    for bet_id, desc, created_at in open_bets:
        try:
            age_days = (now - datetime.fromisoformat(created_at)).days
        except (ValueError, TypeError):
            age_days = 0
        bets_info.append({"id": bet_id, "description": desc, "age_days": age_days})

    # Hours since last agent action in this chat
    last_action = conn.execute(
        "SELECT executed_at FROM agent_actions WHERE chat_id = ? ORDER BY executed_at DESC LIMIT 1;",
        (chat_id,),
    ).fetchone()
    if last_action:
        try:
            last_dt = datetime.fromisoformat(last_action[0])
            hours_since_last = (now - last_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            hours_since_last = 999
    else:
        hours_since_last = 999  # never acted

    # Group theme
    group_theme = get_group_theme(conn, chat_id)

    return {
        "chat_id": chat_id,
        "current_time_utc": now.isoformat(),
        "day_of_week": now.strftime("%A"),
        "message_counts": counts,
        "active_users_6h": active_users,
        "recent_messages": recent_snippets,
        "open_bets": bets_info,
        "hours_since_last_bot_action": round(hours_since_last, 1),
        "group_theme": group_theme or "(no theme profile yet)",
    }


# ============================================================
# Reason — system prompt is built from the tool registry
# ============================================================

GLOBAL_GUIDELINES = [
    "If the bot acted less than 4 hours ago, almost always choose \"nothing\".",
    "If there are fewer than 10 messages in the last 24h, the chat is quiet — choose \"nothing\".",
]


def build_system_prompt() -> str:
    """Generate the agent system prompt from the tool registry."""
    action_lines = ["- nothing: Stay quiet. Use this most of the time."]
    guideline_lines = list(GLOBAL_GUIDELINES)

    for name, tool in TOOLS.items():
        line = f"- {name}: {tool['description']}"
        if tool.get("cost"):
            line += f" Costs ~${tool['cost']:.2f}."
        action_lines.append(line)
        if tool.get("guidelines"):
            guideline_lines.append(f"- {name}: {tool['guidelines']}")

    return (
        "You are OTLCBot's decision engine. You observe a group chat's current state "
        "and decide what the bot should do right now. You should usually choose \"nothing\" — "
        "only act when there's a genuine reason.\n\n"
        "Available actions:\n"
        + "\n".join(action_lines) + "\n\n"
        "Guidelines:\n"
        + "\n".join(f"- {g}" if not g.startswith("- ") else g for g in guideline_lines) + "\n\n"
        "Respond with ONLY valid JSON:\n"
        '{"action": "<action_name>", "reason": "<one sentence explaining why>", "params": {}}'
    )


def reason(context: dict) -> dict:
    """Ask Gemini to decide what the bot should do given the current context."""
    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)

        system_prompt = build_system_prompt()

        # Build a readable context string for the model
        ctx_text = (
            f"Day: {context['day_of_week']}, Time (UTC): {context['current_time_utc']}\n"
            f"Messages — last 6h: {context['message_counts']['6h']}, "
            f"last 24h: {context['message_counts']['24h']}, "
            f"last 7d: {context['message_counts']['7d']}\n"
            f"Active users (6h): {', '.join(context['active_users_6h']) or 'none'}\n"
            f"Hours since last bot action: {context['hours_since_last_bot_action']}\n"
            f"Open bets: {json.dumps(context['open_bets']) if context['open_bets'] else 'none'}\n"
            f"\nGroup personality: {context['group_theme']}\n"
            f"\nRecent messages:\n" + "\n".join(context['recent_messages'][-10:])
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=f"{system_prompt}\n\n--- CURRENT STATE ---\n{ctx_text}",
            config={"max_output_tokens": 150},
        )

        raw = (response.text or "").strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        decision = json.loads(raw)
        # Validate structure
        if "action" not in decision:
            decision = {"action": "nothing", "reason": "malformed response", "params": {}}
        if "params" not in decision:
            decision["params"] = {}
        if "reason" not in decision:
            decision["reason"] = ""

        return decision

    except Exception as e:
        print(f"  Agent reasoning failed: {e}")
        return {"action": "nothing", "reason": f"reasoning error: {e}", "params": {}}


# ============================================================
# Act — dispatch from registry
# ============================================================

async def execute(conn: sqlite3.Connection, chat_id: int, decision: dict, bot) -> bool:
    """Execute the agent's chosen action via the tool registry. Returns True if something was sent."""
    action = decision["action"]

    if action == "nothing":
        return False

    tool = TOOLS.get(action)
    if not tool:
        print(f"  Agent chose unknown action: {action}")
        return False

    return await tool["execute"](conn, chat_id, bot, decision.get("params", {}))


# ============================================================
# Registered Tools
# ============================================================

@register_tool(
    name="send_commentary",
    description="Send a brief, natural observation about recent conversation. "
                "Only if the chat has been active and the bot hasn't spoken in a while.",
    guidelines="Commentary should be rare and genuinely relevant, not forced.",
)
async def tool_send_commentary(conn, chat_id, bot, params):
    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)

        recent = conn.execute(
            """
            SELECT COALESCE(username, full_name, 'unknown') AS who, text
            FROM messages
            WHERE chat_id = ? AND text IS NOT NULL AND LENGTH(TRIM(text)) >= 5
            ORDER BY sent_at_utc DESC
            LIMIT 20;
            """,
            (chat_id,),
        ).fetchall()
        snippets = "\n".join(f"{who}: {text[:150]}" for who, text in reversed(recent) if text)

        group_theme = get_group_theme(conn, chat_id) or ""

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                "You are OTLCBot, a wry and observant group chat bot. Based on the recent "
                "conversation below, write ONE brief message (1-2 sentences max) that makes "
                "a genuine observation about what's being discussed. Be natural, not forced. "
                "Don't be cringe. Don't use emojis excessively. Match the group's tone.\n\n"
                f"Group personality: {group_theme}\n\n"
                f"Recent messages:\n{snippets}"
            ),
            config={"max_output_tokens": 80},
        )
        commentary = (response.text or "").strip()
        if commentary:
            await bot.send_message(chat_id=chat_id, text=commentary)
            return True
        return False

    except Exception as e:
        print(f"  Commentary generation failed: {e}")
        return False


@register_tool(
    name="generate_cartoon",
    description="Create an illustrated cartoon about recent conversation. "
                "Only for especially active or funny weeks.",
    guidelines="Don't generate cartoons more than once a week.",
    cost=0.04,
)
async def tool_generate_cartoon(conn, chat_id, bot, params):
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    snippets = get_weekly_snippets(conn, chat_id, since)
    if snippets:
        theme = get_group_theme(conn, chat_id) or ""
        image_bytes, image_prompt = generate_weekly_image(snippets, context=theme)
        if image_bytes:
            week_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            sent_msg = await bot.send_photo(chat_id=chat_id, photo=io.BytesIO(image_bytes))
            if sent_msg.photo:
                conn.execute(
                    "INSERT INTO weekly_images (chat_id, week_of, image_prompt, telegram_file_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?);",
                    (chat_id, week_of, image_prompt, sent_msg.photo[-1].file_id,
                     datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
            return True
    return False


@register_tool(
    name="nudge_bet",
    description="Remind the group about an open bet that's getting stale (>14 days old). "
                "Include the bet ID in params.",
    guidelines="Bet nudges are useful when a bet is >14 days old and the group seems to have forgotten.",
)
async def tool_nudge_bet(conn, chat_id, bot, params):
    bet_id = params.get("bet_id")
    if bet_id:
        row = conn.execute(
            "SELECT description, wager, created_by_name FROM bets WHERE id = ? AND chat_id = ? AND settled_at IS NULL;",
            (bet_id, chat_id),
        ).fetchone()
        if row:
            desc, wager, by_name = row
            msg = (
                f"🎲 Bet check-in — #{bet_id} is still open!\n\n"
                f"{desc}\n"
                f"💰 {wager} (by @{by_name})\n\n"
                f"Time to settle up? Use /settlebet {bet_id} <winner>"
            )
            await bot.send_message(chat_id=chat_id, text=msg)
            return True
    # Fallback: nudge the oldest open bet
    row = conn.execute(
        "SELECT id, description, wager, created_by_name FROM bets WHERE chat_id = ? AND settled_at IS NULL ORDER BY created_at ASC LIMIT 1;",
        (chat_id,),
    ).fetchone()
    if row:
        bid, desc, wager, by_name = row
        msg = (
            f"🎲 Bet check-in — #{bid} is still open!\n\n"
            f"{desc}\n"
            f"💰 {wager} (by @{by_name})\n\n"
            f"Time to settle up? Use /settlebet {bid} <winner>"
        )
        await bot.send_message(chat_id=chat_id, text=msg)
        return True
    return False


@register_tool(
    name="sincerity_check",
    description="Run a DFW sincerity analysis and share results. "
                "Only if there's been substantial conversation this week.",
)
async def tool_sincerity_check(conn, chat_id, bot, params):
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    sincerity_snippets = get_sincerity_snippets(conn, chat_id, since, SINCERITY_SNIPPET_LIMIT)
    if sincerity_snippets:
        data = analyze_sincerity(sincerity_snippets)
        if data:
            week_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            msg = build_group_sincerity_message(conn, chat_id, data, week_of)
            save_sincerity_scores(conn, chat_id, week_of, data)
            conn.commit()
            await bot.send_message(chat_id=chat_id, text=msg)
            return True
    return False


@register_tool(
    name="weekly_report",
    description="Generate the full weekly report with stats, recap, and optional cartoon. "
                "Usually only on Fridays or when forced.",
)
async def tool_weekly_report(conn, chat_id, bot, params):
    _ensure_profile_tables(conn)

    text = build_weekly_report(chat_id)
    week_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    # Sincerity index
    if ENABLE_SINCERITY_INDEX and GEMINI_API_KEY:
        sincerity_snippets = get_sincerity_snippets(conn, chat_id, since, SINCERITY_SNIPPET_LIMIT)
        if sincerity_snippets:
            sincerity_data = analyze_sincerity(sincerity_snippets)
            if sincerity_data:
                group_msg = build_group_sincerity_message(conn, chat_id, sincerity_data, week_of)
                text += "\n\n" + group_msg
                save_sincerity_scores(conn, chat_id, week_of, sincerity_data)
                conn.commit()

    # Group theme + cartoon
    image_bytes = None
    image_prompt = None
    if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
        img_snippets = get_weekly_snippets(conn, chat_id, since)
        if img_snippets:
            group_theme = update_group_theme(conn, chat_id, img_snippets)
            image_bytes, image_prompt = generate_weekly_image(img_snippets, context=group_theme)

    # Send
    if image_bytes:
        sent_msg = await bot.send_photo(chat_id=chat_id, photo=io.BytesIO(image_bytes))
        if sent_msg.photo:
            conn.execute(
                "INSERT INTO weekly_images (chat_id, week_of, image_prompt, telegram_file_id, created_at) "
                "VALUES (?, ?, ?, ?, ?);",
                (chat_id, week_of, image_prompt, sent_msg.photo[-1].file_id,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    await bot.send_message(chat_id=chat_id, text=text)
    return True


@register_tool(
    name="add_media",
    description="Someone in the chat organically recommended a movie, show, or book "
                "(e.g. \"you guys should watch X\", \"just finished reading Y, it's amazing\"). "
                "Extract the title and add it to the group's Watch/Read list.",
    guidelines="Only fire when someone clearly recommends something, not when they casually "
               "mention a title in passing. Look for recommendation language.",
)
async def tool_add_media(conn, chat_id, bot, params):
    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)

        # Get recent messages to scan for recommendations
        recent = conn.execute(
            """
            SELECT COALESCE(username, full_name, 'unknown') AS who,
                   user_id, text
            FROM messages
            WHERE chat_id = ? AND text IS NOT NULL AND LENGTH(TRIM(text)) >= 5
            ORDER BY sent_at_utc DESC
            LIMIT 25;
            """,
            (chat_id,),
        ).fetchall()
        snippets = "\n".join(f"{who}: {text[:200]}" for who, _, text in reversed(recent) if text)

        # Build a lookup of username -> user_id from the recent messages
        user_id_map = {}
        for who, uid, _ in recent:
            if uid and who:
                user_id_map[who] = uid

        # Get existing watchlist titles to avoid duplicates
        existing = conn.execute(
            "SELECT LOWER(title) FROM watchlist WHERE chat_id = ?;",
            (chat_id,),
        ).fetchall()
        existing_titles = {row[0] for row in existing}

        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=(
                "Analyze these group chat messages and find any media recommendations — "
                "someone suggesting a movie, TV show, or book for others to watch or read. "
                "Only extract GENUINE recommendations, not passing mentions.\n\n"
                "If you find a recommendation, respond with ONLY valid JSON:\n"
                '{"found": true, "title": "<title>", "media_type": "Movie|Book|Show", '
                '"recommended_by": "<username who recommended it>"}\n\n'
                "If there is no clear recommendation, respond with:\n"
                '{"found": false}\n\n'
                f"Already on the list (don't duplicate): {', '.join(existing_titles) if existing_titles else 'nothing yet'}\n\n"
                f"Messages:\n{snippets}"
            ),
            config={"max_output_tokens": 100},
        )

        raw = (response.text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(raw)

        if not result.get("found"):
            print("  add_media: no recommendation found in recent messages")
            return False

        title = result.get("title", "").strip()
        media_type = result.get("media_type", "Other").strip()
        recommended_by = result.get("recommended_by", "unknown").strip()

        if not title:
            return False

        # Check for duplicate (case-insensitive)
        if title.lower() in existing_titles:
            print(f"  add_media: '{title}' already on the list, skipping")
            return False

        # Normalize media_type
        if media_type not in ("Movie", "Book", "Show", "Podcast", "Article"):
            media_type = "Other"

        # Look up user_id for the recommender
        recommender_id = user_id_map.get(recommended_by, 0)

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO watchlist (chat_id, title, media_type, added_by_id, added_by_username, added_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (chat_id, title, media_type, recommender_id, recommended_by, now),
        )
        conn.commit()

        # Notify the group
        type_icons = {"Movie": "\U0001F3AC", "Book": "\U0001F4D6", "Show": "\U0001F4FA",
                      "Podcast": "\U0001F3A7", "Article": "\U0001F4F0", "Other": "\U0001F517"}
        icon = type_icons.get(media_type, "\U0001F517")
        msg = (
            f"{icon} Added to the Watch/Read list!\n\n"
            f"**{title}** ({media_type})\n"
            f"Recommended by @{recommended_by}\n\n"
            f"Rate it with /rate — see the full list with /watchlist or on the /dashboard"
        )
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        print(f"  add_media: added '{title}' ({media_type}) recommended by @{recommended_by}")
        return True

    except Exception as e:
        print(f"  add_media failed: {e}")
        return False


# ============================================================
# Agent loop
# ============================================================

def _log_action(conn: sqlite3.Connection, chat_id: int, action: str, reason: str, success: bool) -> None:
    """Record an agent decision in the agent_actions table."""
    conn.execute(
        "INSERT INTO agent_actions (chat_id, action, reason, executed_at, success) VALUES (?, ?, ?, ?, ?);",
        (chat_id, action, reason, datetime.now(timezone.utc).isoformat(), 1 if success else 0),
    )
    conn.commit()


async def run_agent_loop(
    chat_ids: list[str],
    bot,
    force_weekly: bool = False,
) -> None:
    """
    Main agent entry point. For each chat:
    1. Gather context
    2. Ask Gemini what to do (or force weekly_report if flag is set)
    3. Execute the chosen action
    4. Log the decision

    Args:
        chat_ids: List of chat ID strings to process.
        bot: Telegram Bot instance.
        force_weekly: If True, override the agent's decision with weekly_report.
    """
    if not GEMINI_API_KEY:
        print("Agent: GEMINI_API_KEY not set, skipping.")
        return

    print(f"Agent: {len(TOOLS)} tools registered: {', '.join(TOOLS.keys())}")

    with sqlite3.connect(DB_PATH) as conn:
        ensure_agent_table(conn)
        _ensure_profile_tables(conn)

        for chat_id_str in chat_ids:
            chat_id = int(chat_id_str)
            print(f"\n--- Agent evaluating chat {chat_id} ---")

            # Observe
            context = gather_context(conn, chat_id)
            print(f"  Messages 6h/24h/7d: {context['message_counts']['6h']}/{context['message_counts']['24h']}/{context['message_counts']['7d']}")
            print(f"  Active users (6h): {context['active_users_6h']}")
            print(f"  Hours since last action: {context['hours_since_last_bot_action']}")
            print(f"  Open bets: {len(context['open_bets'])}")

            if force_weekly:
                # Weekly cron: still let agent see the state, but override to weekly_report
                decision = {"action": "weekly_report", "reason": "scheduled weekly run", "params": {}}
                print(f"  Decision (forced): weekly_report")
            else:
                # Reason
                decision = reason(context)
                print(f"  Decision: {decision['action']} — {decision.get('reason', '')}")

            # Act
            if decision["action"] != "nothing":
                try:
                    success = await execute(conn, chat_id, decision, bot)
                    _log_action(conn, chat_id, decision["action"], decision.get("reason", ""), success)
                    print(f"  Executed: {decision['action']} (success={success})")
                except Exception as e:
                    _log_action(conn, chat_id, decision["action"], f"error: {e}", False)
                    print(f"  Execution failed: {e}")
            else:
                _log_action(conn, chat_id, "nothing", decision.get("reason", ""), True)
                print(f"  No action taken.")

    # Send admin cost/health DM on weekly runs
    if force_weekly:
        # Rough cost estimate: 1 reason call per chat + whatever each action cost
        estimated_text_calls = len(chat_ids) * 5  # conservative estimate
        estimated_images = len(chat_ids)           # one cartoon per chat
        await _send_cost_dm(bot, estimated_images, estimated_text_calls)
