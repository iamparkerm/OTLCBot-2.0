import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, WebAppInfo
from telegram.ext import (
    Application, MessageHandler, CommandHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=ROOT / ".env")

# ==========================================================================
# Group Structure  (see weekly.py for full docs)
# ==========================================================================
# 1. Penetr8in' Experiences  -1003792615572   (standalone)
# 2. Owl Town (combined report → Omelas Basement -1001320128437):
#      Omelas Basement -1001320128437   Insta(Tele)gram -1001789253890
#      Books -952331006    AI -4737782983    Health -339793553    Jocks -876016974
# ==========================================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", str(ROOT / "data.db"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ENABLE_AGENT = os.getenv("ENABLE_AGENT", "false").lower() == "true"
AGENT_MSG_THRESHOLD = int(os.getenv("AGENT_MSG_THRESHOLD", "50"))

# In-memory counter: messages since last agent run, per chat
_msg_counter: dict[int, int] = {}

# Conversation states for /bet
BET_DESCRIPTION, BET_SETTLEMENT, BET_WAGER = range(3)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                sent_at_utc TEXT NOT NULL,
                text TEXT
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_id, sent_at_utc);"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                created_by_id INTEGER NOT NULL,
                created_by_name TEXT,
                description TEXT NOT NULL,
                settlement TEXT NOT NULL,
                wager TEXT NOT NULL,
                created_at TEXT NOT NULL,
                settled_at TEXT,
                winner TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sincerity_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                week_of TEXT NOT NULL,
                username TEXT NOT NULL,
                user_id INTEGER,
                irony_pct REAL NOT NULL,
                grade TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sincerity_chat_week ON sincerity_scores(chat_id, week_of);"
        )
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
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                media_type TEXT NOT NULL,
                added_by_id INTEGER NOT NULL,
                added_by_username TEXT,
                added_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_completions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL REFERENCES watchlist(id),
                user_id INTEGER NOT NULL,
                completed_at TEXT NOT NULL,
                rating INTEGER,
                UNIQUE(item_id, user_id)
            );
            """
        )
        # Migration: add rating column if missing
        try:
            conn.execute("ALTER TABLE watchlist_completions ADD COLUMN rating INTEGER;")
        except sqlite3.OperationalError:
            pass  # column already exists
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ OTLCBot is running and logging messages.")


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"chat_id: {update.effective_chat.id}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(username, full_name, 'unknown') AS who, COUNT(*) AS cnt
            FROM messages
            WHERE chat_id = ?
              AND sent_at_utc >= datetime('now', '-1 day')
            GROUP BY who
            ORDER BY cnt DESC
            LIMIT 10;
            """,
            (chat_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("No messages logged in the last 24 hours.")
        return

    lines = ["📊 Top posters (last 24h):"]
    for who, cnt in rows:
        lines.append(f"- {who}: {cnt}")
    await update.message.reply_text("\n".join(lines))


# ---------- /bet conversation ----------
async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🎲 New bet! What's the bet? (or /cancel to abort)")
    return BET_DESCRIPTION


async def bet_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["bet_description"] = update.message.text
    await update.message.reply_text("📅 When or how does it settle?")
    return BET_SETTLEMENT


async def bet_settlement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["bet_settlement"] = update.message.text
    await update.message.reply_text("💰 What's the wager?")
    return BET_WAGER


async def bet_wager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    description = context.user_data["bet_description"]
    settlement = context.user_data["bet_settlement"]
    wager = update.message.text

    user = update.effective_user
    chat_id = update.effective_chat.id
    created_at = update.message.date.astimezone(timezone.utc).isoformat()
    created_by_name = user.username or f"{user.first_name or ''} {user.last_name or ''}".strip()

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO bets (chat_id, created_by_id, created_by_name, description, settlement, wager, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (chat_id, user.id, created_by_name, description, settlement, wager, created_at),
        )
        bet_id = cur.lastrowid

    await update.message.reply_text(
        f"✅ Bet #{bet_id} recorded!\n\n"
        f"🎲 {description}\n"
        f"📅 Settles: {settlement}\n"
        f"💰 Wager: {wager}\n"
        f"👤 By: @{created_by_name}"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def bet_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Bet cancelled.")
    return ConversationHandler.END


# ---------- /bets ----------
async def bets_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, description, settlement, wager, created_by_name
            FROM bets
            WHERE chat_id = ? AND settled_at IS NULL
            ORDER BY id;
            """,
            (chat_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("No open bets! Use /bet to create one.")
        return

    lines = ["🎲 Open bets:"]
    for bet_id, desc, settle, wager, by_name in rows:
        lines.append(f"#{bet_id}: {desc}\n   📅 {settle} | 💰 {wager} | 👤 @{by_name}")
    await update.message.reply_text("\n\n".join(lines))


# ---------- /settlebet ----------
async def settlebet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /settlebet <id> <winner>\nExample: /settlebet 1 @parker")
        return

    try:
        bet_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Bet ID must be a number. Example: /settlebet 1 @parker")
        return

    winner = " ".join(context.args[1:])
    chat_id = update.effective_chat.id
    settled_at = update.message.date.astimezone(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, description FROM bets WHERE id = ? AND chat_id = ? AND settled_at IS NULL;",
            (bet_id, chat_id),
        ).fetchone()

        if not row:
            await update.message.reply_text(f"Bet #{bet_id} not found or already settled.")
            return

        conn.execute(
            "UPDATE bets SET settled_at = ?, winner = ? WHERE id = ?;",
            (settled_at, winner, bet_id),
        )

    await update.message.reply_text(f"🏆 Bet #{bet_id} settled!\n\n🎲 {row[1]}\n🥇 Winner: {winner}")


# ---------- /gallery ----------
async def gallery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM weekly_images WHERE chat_id = ?;",
            (chat_id,),
        ).fetchone()[0]

    if total == 0:
        await update.message.reply_text("No cartoons yet! They'll appear after the next weekly report.")
        return

    # Show the newest image (last index)
    await _send_gallery_page(update.message, chat_id, total - 1, total, edit=False)


async def _send_gallery_page(target, chat_id: int, index: int, total: int, edit: bool = False) -> None:
    """Send or edit a gallery page showing image at `index` (0 = oldest)."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT week_of, image_prompt, telegram_file_id FROM weekly_images "
            "WHERE chat_id = ? ORDER BY week_of ASC, id ASC LIMIT 1 OFFSET ?;",
            (chat_id, index),
        ).fetchone()

    if not row:
        return

    week_of, prompt, file_id = row
    caption = f"Week of {week_of}"
    if prompt:
        caption += f"\n{prompt[:200]}"

    # Build navigation buttons
    buttons = []
    if index > 0:
        buttons.append(InlineKeyboardButton("< Prev", callback_data=f"gallery:{chat_id}:{index - 1}"))
    buttons.append(InlineKeyboardButton(f"{index + 1}/{total}", callback_data="gallery:noop"))
    if index < total - 1:
        buttons.append(InlineKeyboardButton("Next >", callback_data=f"gallery:{chat_id}:{index + 1}"))
    keyboard = InlineKeyboardMarkup([buttons])

    if edit:
        try:
            await target.edit_message_media(
                media=InputMediaPhoto(media=file_id, caption=caption),
                reply_markup=keyboard,
            )
        except Exception:
            pass  # message may have expired
    else:
        await target.reply_photo(photo=file_id, caption=caption, reply_markup=keyboard)


async def gallery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "gallery:noop":
        return

    parts = data.split(":")
    if len(parts) != 3:
        return

    _, chat_id_str, index_str = parts
    chat_id = int(chat_id_str)

    # Security: only allow gallery browsing for the current chat
    if chat_id != update.effective_chat.id:
        return

    index = int(index_str)
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM weekly_images WHERE chat_id = ?;",
            (chat_id,),
        ).fetchone()[0]

    if index < 0 or index >= total:
        return

    await _send_gallery_page(query.message, chat_id, index, total, edit=True)


# ---------- /dashboard (Telegram WebApp) ----------
async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not WEBAPP_URL:
        await update.message.reply_text(
            "Dashboard not configured yet.\n(Set WEBAPP_URL in .env after running the web server.)"
        )
        return

    chat_id = update.effective_chat.id
    url = f"{WEBAPP_URL.rstrip('/')}?chat_id={chat_id}"

    # Try WebApp button first (opens as Telegram MiniApp), fall back to URL button
    try:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Open Dashboard", web_app=WebAppInfo(url=url))
        ]])
        await update.message.reply_text("📊 OTLC Dashboard", reply_markup=keyboard)
    except Exception as e:
        print(f"[dashboard] WebApp button failed: {type(e).__name__}: {e}")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Open Dashboard", url=url)
        ]])
        await update.message.reply_text("📊 OTLC Dashboard", reply_markup=keyboard)


async def log_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    text = msg.text or msg.caption
    if not text:
        return

    user = msg.from_user
    sent_at = msg.date
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    sent_at_utc = sent_at.astimezone(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO messages
            (chat_id, message_id, user_id, username, full_name, sent_at_utc, text)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                msg.chat_id,
                msg.message_id,
                user.id if user else None,
                user.username if user else None,
                f"{user.first_name or ''} {user.last_name or ''}".strip() if user else None,
                sent_at_utc,
                text,
            ),
        )

    # Agent trigger: run agent loop after AGENT_MSG_THRESHOLD messages
    if ENABLE_AGENT:
        chat_id = msg.chat_id
        _msg_counter[chat_id] = _msg_counter.get(chat_id, 0) + 1
        if _msg_counter[chat_id] >= AGENT_MSG_THRESHOLD:
            _msg_counter[chat_id] = 0
            # Schedule agent loop as a background task (non-blocking)
            import asyncio
            asyncio.ensure_future(_run_agent_for_chat(str(chat_id), context))


# ---------- /watch & /read (Watchlist) ----------
MEDIA_TYPE_MAP = {"watch": "Movie", "read": "Book"}


async def watchlist_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /watch <title> and /read <title> commands."""
    command = update.message.text.split()[0].lstrip("/").lower()  # "watch" or "read"
    media_type = MEDIA_TYPE_MAP.get(command, "Other")

    title = " ".join(context.args).strip() if context.args else ""
    if not title:
        emoji = "\U0001F3AC" if command == "watch" else "\U0001F4D6"
        await update.message.reply_text(
            f"{emoji} Usage: /{command} <title>\n"
            f"Example: /{command} The Bear"
        )
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (chat_id, title, media_type, added_by_id, added_by_username, added_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (chat_id, title, media_type, user.id, user.username or user.first_name, now),
        )

    emoji = "\U0001F3AC" if media_type == "Movie" else "\U0001F4D6"
    await update.message.reply_text(
        f"{emoji} Added to the list!\n"
        f"**{title}** ({media_type})\n"
        f"Added by @{user.username or user.first_name}",
        parse_mode="Markdown",
    )


# ---------- /rate (Watchlist rating) ----------
async def watchlist_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /rate <id> <1-5> — rate a watchlist item."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "\u2B50 Usage: /rate <id> <1-5>\n"
            "Example: /rate 3 5\n\n"
            "Use /watchlist to see item IDs."
        )
        return

    try:
        item_id = int(context.args[0])
        rating = int(context.args[1])
    except ValueError:
        await update.message.reply_text("\u274C ID and rating must be numbers.")
        return

    if rating < 1 or rating > 5:
        await update.message.reply_text("\u274C Rating must be 1\u20135.")
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT title FROM watchlist WHERE id = ? AND chat_id = ?;",
            (item_id, chat_id),
        ).fetchone()
        if not row:
            await update.message.reply_text("\u274C Item not found in this chat.")
            return

        conn.execute(
            """
            INSERT INTO watchlist_completions (item_id, user_id, completed_at, rating)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(item_id, user_id) DO UPDATE SET rating = excluded.rating, completed_at = excluded.completed_at;
            """,
            (item_id, user.id, now, rating),
        )

    stars = "\u2B50" * rating
    await update.message.reply_text(
        f"{stars} Rated **{row[0]}** {rating}/5!",
        parse_mode="Markdown",
    )


# ---------- /watchlist (show list with IDs) ----------
async def watchlist_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the watchlist with item IDs for rating."""
    chat_id = update.effective_chat.id

    with sqlite3.connect(DB_PATH) as conn:
        items = conn.execute(
            "SELECT id, title, media_type FROM watchlist WHERE chat_id = ? ORDER BY added_at DESC;",
            (chat_id,),
        ).fetchall()

    if not items:
        await update.message.reply_text("\U0001F4DA No items on the watch/read list yet.\nUse /watch or /read to add one!")
        return

    type_icons = {"Movie": "\U0001F3AC", "Book": "\U0001F4D6", "Show": "\U0001F4FA",
                  "Podcast": "\U0001F3A7", "Article": "\U0001F4F0", "Other": "\U0001F517"}
    lines = ["\U0001F4DA **Watch/Read List**\n"]
    for item_id, title, media_type in items:
        icon = type_icons.get(media_type, "\U0001F517")
        lines.append(f"`{item_id}.` {icon} {title}")
    lines.append("\nRate with: /rate <id> <1\u20135>")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------- /help ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available commands."""
    await update.message.reply_text(
        "\U0001F916 **OTLCBot Commands**\n\n"
        "\U0001F4CA /dashboard \u2014 Open the OTLC Dashboard\n"
        "\U0001F4CA /stats \u2014 Message stats for this chat\n"
        "\n"
        "\U0001F3B2 /bet \u2014 Create a new bet\n"
        "\U0001F3B2 /bets \u2014 Show open bets\n"
        "\U0001F3B2 /settlebet <id> <winner> \u2014 Settle a bet\n"
        "\n"
        "\U0001F3AC /watch <title> \u2014 Add a movie/show to watch\n"
        "\U0001F4D6 /read <title> \u2014 Add a book to read\n"
        "\U0001F4DA /watchlist \u2014 Show list with IDs\n"
        "\u2B50 /rate <id> <1\u20135> \u2014 Rate an item\n"
        "\n"
        "\U0001F5BC /gallery \u2014 Browse weekly cartoons\n"
        "\U0001F194 /chatid \u2014 Show this chat's ID",
        parse_mode="Markdown",
    )


async def _run_agent_for_chat(chat_id_str: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the agent loop for a single chat (called from message-count trigger)."""
    try:
        from agent import run_agent_loop
        await run_agent_loop([chat_id_str], context.bot)
    except Exception as e:
        print(f"Agent loop failed for chat {chat_id_str}: {e}")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")

    init_db()

    app = Application.builder().token(TOKEN).build()

    # Bet conversation handler (must be added before the catch-all message handler)
    bet_conv = ConversationHandler(
        entry_points=[CommandHandler("bet", bet_start)],
        states={
            BET_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, bet_description)],
            BET_SETTLEMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bet_settlement)],
            BET_WAGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, bet_wager)],
        },
        fallbacks=[CommandHandler("cancel", bet_cancel)],
    )
    app.add_handler(bet_conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("bets", bets_list))
    app.add_handler(CommandHandler("settlebet", settlebet))
    app.add_handler(CommandHandler("gallery", gallery))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("watch", watchlist_add))
    app.add_handler(CommandHandler("read", watchlist_add))
    app.add_handler(CommandHandler("rate", watchlist_rate))
    app.add_handler(CommandHandler("watchlist", watchlist_show))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(gallery_callback, pattern=r"^gallery:"))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, log_message))

    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as exc:
        if "Conflict" in type(exc).__name__ or "terminated by other getUpdates" in str(exc):
            print("Conflict: another bot instance detected. Exiting cleanly (no restart).")
            sys.exit(0)
        raise


if __name__ == "__main__":
    main()
