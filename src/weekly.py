"""
Weekly pipeline orchestrator.

Runs the Friday report for Owl Town and/or Penetr8in.
All capability logic lives in config, reports, profiles, and sincerity modules.

Usage:
    python src/weekly.py                   # run everything
    python src/weekly.py --group owltown   # Owl Town only (3 PM)
    python src/weekly.py --group penetr8in # Penetr8in only (4 PM)
"""

import asyncio
import io
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Bot

from config import (
    ADMIN_USERNAME,
    ADMIN_USER_ID,
    CHAT_IDS,
    COST_PER_IMAGE,
    COST_PER_TEXT_CALL,
    DB_PATH,
    ENABLE_AI_SUMMARY,
    ENABLE_SINCERITY_INDEX,
    GEMINI_API_KEY,
    OWL_TOWN_CHAT_IDS,
    OWL_TOWN_NAMES,
    OWL_TOWN_SEND_TO,
    SINCERITY_SNIPPET_LIMIT,
    TOKEN,
)
from profiles import (
    _check_dossier_milestone,
    ensure_profile_tables,
    generate_case_file_text,
    get_group_theme,
    get_user_snippets,
    update_group_theme,
    update_user_profile,
)
from reports import (
    build_grounding_block,
    build_owl_town_report,
    build_weekly_gazette,
    build_weekly_report,
    generate_weekly_image,
    get_conversation_windows,
    get_conversation_windows_multi,
)
from sincerity import (
    analyze_sincerity,
    build_group_sincerity_message,
    build_user_dm,
    get_sincerity_snippets,
    save_sincerity_scores,
)


# DB bootstrap lives in profiles.ensure_profile_tables


# ============================================================
# Profile refresh (Editor responsibility)
# ============================================================

async def _refresh_all_profiles(
    conn: sqlite3.Connection,
    chat_ids: list[int],
    bot,
    week_of: str,
    notify_chat_id: int,
) -> int:
    """Refresh profiles for ALL users active in the given chats over the past 14 days.

    This runs independently of the sincerity analysis so every active member gets
    a current case file — not just those who appeared in the sincerity pass.
    Returns the number of Gemini text calls made.
    """
    if not (ENABLE_AI_SUMMARY and GEMINI_API_KEY):
        return 0

    since_iso = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    placeholders = ",".join("?" * len(chat_ids))

    active_users = conn.execute(
        f"""SELECT DISTINCT m.user_id,
               COALESCE(m.username, m.full_name, 'unknown') AS display_name
            FROM messages m
            WHERE m.chat_id IN ({placeholders})
              AND m.sent_at_utc >= ?
              AND m.user_id IS NOT NULL
            ORDER BY display_name COLLATE NOCASE""",
        chat_ids + [since_iso],
    ).fetchall()

    text_calls = 0
    print(f"  Profile refresh: {len(active_users)} active user(s) across {len(chat_ids)} chat(s)")

    for user_id, display_name in active_users:
        try:
            # Gather up to 30 recent messages from all chats combined
            snippet_rows = conn.execute(
                f"""SELECT COALESCE(username, full_name, 'unknown') || ': ' || text
                    FROM messages
                    WHERE chat_id IN ({placeholders})
                      AND user_id = ?
                      AND sent_at_utc >= ?
                      AND text IS NOT NULL
                      AND LENGTH(TRIM(text)) >= 5
                    ORDER BY sent_at_utc DESC LIMIT 30""",
                chat_ids + [user_id, since_iso],
            ).fetchall()

            if not snippet_rows:
                continue

            snippets = "\n".join(r[0] for r in snippet_rows)
            user_profile = update_user_profile(conn, user_id, display_name, snippets)
            text_calls += 1
            print(f"    Refreshed: {display_name} ({len(user_profile)} chars)")

            ver_row = conn.execute(
                "SELECT version FROM user_profiles WHERE user_id = ?;", (user_id,)
            ).fetchone()
            version = ver_row[0] if ver_row else 1

            # Pull the most recent sincerity score for irony colouring
            irony_row = conn.execute(
                """SELECT irony_pct FROM sincerity_scores
                   WHERE username = ?
                   ORDER BY week_of DESC LIMIT 1;""",
                (display_name,),
            ).fetchone()
            irony_pct = float(irony_row[0]) if irony_row else 0.0

            generate_case_file_text(conn, user_id, display_name, user_profile, version,
                                    irony_pct=irony_pct)
            text_calls += 1

            milestone_msg = _check_dossier_milestone(version, display_name)
            if milestone_msg:
                try:
                    await bot.send_message(chat_id=notify_chat_id, text=milestone_msg)
                except Exception as e:
                    print(f"    Milestone announcement failed for {display_name}: {e}")

        except Exception as e:
            print(f"    Profile refresh failed for {display_name}: {e}")

    return text_calls


# ============================================================
# Admin cost DM + health check
# ============================================================

def _get_system_health() -> str:
    """Read Pi system health from /proc and /sys (Linux only)."""
    lines = []
    try:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp_c = int(f.read().strip()) / 1000
                temp_warning = " ⚠️" if temp_c >= 70 else ""
                lines.append(f"  🌡 CPU temp: {temp_c:.1f}°C{temp_warning}")
        except (FileNotFoundError, ValueError):
            pass

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

        try:
            stat = os.statvfs("/")
            total_gb = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
            free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
            used_gb = total_gb - free_gb
            pct = (used_gb / total_gb * 100) if total_gb > 0 else 0
            disk_warning = " ⚠️" if pct >= 90 else ""
            lines.append(f"  💿 Disk: {used_gb:.1f}/{total_gb:.1f} GB ({pct:.0f}% used){disk_warning}")
        except (AttributeError, OSError):
            pass

        try:
            with open("/proc/loadavg") as f:
                parts = f.read().strip().split()
                load_1, load_5, load_15 = parts[0], parts[1], parts[2]
                lines.append(f"  ⚡ Load avg: {load_1} / {load_5} / {load_15} (1/5/15 min)")
        except (FileNotFoundError, ValueError):
            pass

        try:
            with open("/proc/uptime") as f:
                uptime_secs = float(f.read().strip().split()[0])
                days = int(uptime_secs // 86400)
                hours = int((uptime_secs % 86400) // 3600)
                lines.append(f"  ⏱ Uptime: {days}d {hours}h")
        except (FileNotFoundError, ValueError):
            pass

        try:
            db_size_mb = DB_PATH.stat().st_size / (1024 * 1024)
            lines.append(f"  🗄 DB size: {db_size_mb:.1f} MB")
        except OSError:
            pass

    except Exception as e:
        lines.append(f"  Health check error: {e}")

    return "\n".join(lines) if lines else "  (health data unavailable — not running on Linux)"


async def _send_cost_dm(bot, images_sent: int, text_calls: int) -> None:
    """DM the admin with a Gemini API cost estimate + Pi health check."""
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
    monthly_est = total * 4.33

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


# ============================================================
# Main orchestrator
# ============================================================

async def send_weekly_async(group: str = "all") -> None:
    """
    group: "all" (default), "owltown", or "penetr8in"
    owltown   — Owl Town combined report only; no cost DM, no wiki rebuild
    penetr8in — Penetr8in standalone reports; runs cost DM and wiki rebuild
    all       — everything in sequence
    """
    if not TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
    if not CHAT_IDS:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID in .env")

    with sqlite3.connect(DB_PATH) as conn:
        ensure_profile_tables(conn)

    bot = Bot(token=TOKEN)
    week_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    images_sent = 0
    text_calls = 0

    owl_town_send_to_int = int(OWL_TOWN_SEND_TO) if OWL_TOWN_SEND_TO else None

    # ── Penetr8in standalone reports ──────────────────────────────────────
    for chat_id_str in (CHAT_IDS if group in ("all", "penetr8in") else []):
        chat_id_int = int(chat_id_str)

        if owl_town_send_to_int and chat_id_int == owl_town_send_to_int:
            print(f"Skipping individual report for {chat_id_int} (will get Owl Town combined)")
            continue

        text = build_weekly_report(chat_id_int)

        # Sincerity Index
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
                        text_calls += 1
                if sincerity_data:
                    group_msg = build_group_sincerity_message(
                        conn, chat_id_int, sincerity_data, week_of
                    )
                    text += "\n\n" + group_msg
                    save_sincerity_scores(conn, chat_id_int, week_of, sincerity_data)

        # Group theme + weekly image
        image_bytes = None
        image_prompt = None
        if ENABLE_AI_SUMMARY and GEMINI_API_KEY:
            since_dt_img = datetime.now(timezone.utc) - timedelta(days=7)
            with sqlite3.connect(DB_PATH) as conn:
                img_snippets = get_conversation_windows(
                    conn, chat_id_int, since_dt_img.isoformat(), max_chars=2500
                )
                if img_snippets:
                    group_theme = update_group_theme(conn, chat_id_int, img_snippets)
                    print(f"  Updated group theme for {chat_id_int} ({len(group_theme)} chars)")
                    grounding = build_grounding_block(conn, chat_id_int)
                    img_context = group_theme + ("\n\n" + grounding if grounding else "")
                    text_calls += 2
                    image_bytes, image_prompt = generate_weekly_image(img_snippets, context=img_context)
                    if image_bytes:
                        time.sleep(20)

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

        # Individual sincerity DMs
        # Profile updates are handled separately by _refresh_all_profiles (below).
        if sincerity_data and sincerity_data.get("users"):
            with sqlite3.connect(DB_PATH) as conn:
                for display_name, irony_pct in sincerity_data["users"].items():
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
                            await bot.send_message(chat_id=row[0], text=dm_text)
                            print(f"  DM sent to {display_name} ({row[0]})")
                        except Exception as e:
                            print(f"  DM to {display_name} failed: {e}")

    # ── Owl Town combined report ───────────────────────────────────────────
    if group in ("all", "owltown") and OWL_TOWN_CHAT_IDS and OWL_TOWN_SEND_TO:
        owl_text = build_owl_town_report()

        # Sincerity across all OT chats
        if ENABLE_SINCERITY_INDEX and GEMINI_API_KEY:
            since_dt = datetime.now(timezone.utc) - timedelta(days=7)
            since = since_dt.isoformat()
            with sqlite3.connect(DB_PATH) as conn:
                all_snippets = []
                for cid_str in OWL_TOWN_CHAT_IDS:
                    s = get_sincerity_snippets(
                        conn, int(cid_str), since,
                        SINCERITY_SNIPPET_LIMIT // len(OWL_TOWN_CHAT_IDS) or 10,
                    )
                    if s:
                        all_snippets.append(s)
                combined = "\n".join(all_snippets)
                if combined:
                    sincerity_data = analyze_sincerity(combined)
                    if sincerity_data:
                        text_calls += 1
                        owl_town_id = 0  # synthetic ID for combined OT tracking
                        group_msg = build_group_sincerity_message(
                            conn, owl_town_id, sincerity_data, week_of
                        )
                        owl_text += "\n\n" + group_msg
                        save_sincerity_scores(conn, owl_town_id, week_of, sincerity_data)

        # Owl Town weekly image
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
                    text_calls += 2
                    owl_image_bytes, owl_image_prompt = generate_weekly_image(
                        owl_img_text, context=owl_context
                    )

        send_to_int = int(OWL_TOWN_SEND_TO)
        if owl_image_bytes:
            sent_msg = await bot.send_photo(
                chat_id=send_to_int, photo=io.BytesIO(owl_image_bytes)
            )
            if sent_msg.photo:
                images_sent += 1
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute(
                        "INSERT INTO weekly_images (chat_id, week_of, image_prompt, telegram_file_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?);",
                        (send_to_int, week_of, owl_image_prompt,
                         sent_msg.photo[-1].file_id, datetime.now(timezone.utc).isoformat()),
                    )

        sincerity_block = ""
        if "\n\n📖 DFW Sincerity Index" in owl_text:
            sincerity_block = owl_text[owl_text.index("\n\n📖 DFW Sincerity Index"):]
        gazette = build_weekly_gazette(owl_text, sincerity_text=sincerity_block)
        if gazette:
            text_calls += 1
            await bot.send_message(chat_id=send_to_int, text=gazette)
            await bot.send_message(chat_id=send_to_int, text=owl_text)
        else:
            await bot.send_message(chat_id=send_to_int, text=owl_text)
        print(f"Sent Owl Town combined report to {send_to_int}")

    # ── Profile refresh (all active users, both groups) ───────────────────
    # Runs after reports/DMs are sent so the wiki rebuild gets fresh case files.
    if group in ("all", "penetr8in"):
        p8_ids = [
            int(c) for c in CHAT_IDS
            if not (owl_town_send_to_int and int(c) == owl_town_send_to_int)
        ]
        if p8_ids:
            print(f"Refreshing profiles for Penetr8in ({len(p8_ids)} chat(s))")
            with sqlite3.connect(DB_PATH) as conn:
                tc = await _refresh_all_profiles(conn, p8_ids, bot, week_of, p8_ids[0])
            text_calls += tc

    if group in ("all", "owltown") and OWL_TOWN_CHAT_IDS:
        ot_ids = [int(c) for c in OWL_TOWN_CHAT_IDS]
        ot_notify = int(OWL_TOWN_SEND_TO) if OWL_TOWN_SEND_TO else ot_ids[0]
        print(f"Refreshing profiles for Owl Town ({len(ot_ids)} chat(s))")
        with sqlite3.connect(DB_PATH) as conn:
            tc = await _refresh_all_profiles(conn, ot_ids, bot, week_of, ot_notify)
        text_calls += tc

    # ── Admin cost DM + wiki rebuild (penetr8in or full run only) ─────────
    if group in ("all", "penetr8in"):
        await _send_cost_dm(bot, images_sent, text_calls)
        try:
            import wiki as wiki_module
            wiki_module.build_wiki(gemini_enabled=bool(GEMINI_API_KEY))
        except Exception as e:
            print(f"[weekly] Wiki build failed (non-fatal): {e}")


# ============================================================
# CLI entry point
# ============================================================

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
