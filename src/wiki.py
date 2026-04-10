"""
OTLCBot Wiki Compiler — builds the public Owl Town research wiki.

Reads from the database (group_themes, case_notes, user_profiles,
sincerity_scores) and optionally calls Gemini to compile articles.
Writes static HTML to WIKI_DIR (default: /opt/otlc/wiki/).

Usage:
    python src/wiki.py              # full build with Gemini
    python src/wiki.py --no-gemini  # data-only build, no API calls

Called automatically at the end of the Friday weekly pipeline.
"""

import html
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=ROOT / ".env")

DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data.db")).expanduser().resolve()
WIKI_DIR = Path(os.getenv("WIKI_DIR", "/opt/otlc/wiki"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

OWL_TOWN_CHATS: dict[str, str] = {
    "-1001320128437": "Omelas Basement",
    "-1001789253890": "Insta(Tele)gram",
    "-952331006":     "Books",
    "-4737782983":    "AI",
    "-339793553":     "Health",
    "-876016974":     "Jocks",
}
OWL_TOWN_HOME = "-1001320128437"

# ============================================================
# Shared style (Wikipedia-ish, matches docs/ pages)
# ============================================================

PAGE_STYLE = """
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Liberation Sans", sans-serif;
           font-size: 15px; line-height: 1.7; color: #222; background: #f8f9fa; }
    .page { max-width: 860px; margin: 0 auto; background: #fff; min-height: 100vh;
            border-left: 1px solid #e0e0e0; border-right: 1px solid #e0e0e0;
            padding: 32px 40px 60px; }
    h1 { font-size: 28px; font-weight: 400; border-bottom: 1px solid #a2a9b1;
         padding-bottom: 4px; margin-bottom: 12px;
         font-family: "Linux Libertine", "Georgia", serif; }
    h2 { font-size: 20px; font-weight: 400; border-bottom: 1px solid #a2a9b1;
         padding-bottom: 2px; margin-top: 28px; margin-bottom: 10px;
         font-family: "Linux Libertine", "Georgia", serif; }
    h3 { font-size: 16px; font-weight: 600; margin-top: 18px; margin-bottom: 6px; }
    p { margin-bottom: 10px; }
    a { color: #0645ad; text-decoration: none; } a:hover { text-decoration: underline; }
    code, pre { font-family: "Consolas", "Liberation Mono", monospace; font-size: 13px; }
    pre { background: #f0f0f0; padding: 14px 16px; border-radius: 4px;
          overflow-x: auto; margin: 10px 0 16px; line-height: 1.5; white-space: pre-wrap; }
    table { border-collapse: collapse; margin: 10px 0 16px; font-size: 14px; width: 100%; }
    th, td { border: 1px solid #a2a9b1; padding: 6px 10px; text-align: left; vertical-align: top; }
    th { background: #eaecf0; font-weight: 600; }
    tr:nth-child(even) { background: #f8f9fa; }
    .breadcrumb { font-size: 13px; color: #555; margin-bottom: 8px; }
    .note-card { border-left: 3px solid #ccc; padding: 8px 12px; margin: 8px 0;
                 background: #fafafa; font-size: 14px; }
    .note-card.commentary { border-color: #22c55e; }
    .note-card.discovery  { border-color: #a855f7; }
    .note-card.observation { border-color: #3b82f6; }
    .note-meta { font-size: 12px; color: #777; margin-top: 4px; }
    .channel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 16px 0; }
    .channel-card { border: 1px solid #e0e0e0; padding: 14px; border-radius: 4px; }
    .channel-card h3 { margin-top: 0; }
    .channel-card .stat { font-size: 13px; color: #555; margin-top: 4px; }
    .grade { display: inline-block; width: 28px; height: 28px; line-height: 28px;
             text-align: center; border-radius: 50%; font-weight: 700; font-size: 13px;
             background: #eaecf0; }
    .grade-A { background: #dcfce7; color: #166534; }
    .grade-B { background: #dbeafe; color: #1e40af; }
    .grade-C { background: #fef9c3; color: #854d0e; }
    .grade-D { background: #ffedd5; color: #9a3412; }
    .grade-F { background: #fee2e2; color: #991b1b; }
    .nav { font-size: 13px; margin-bottom: 20px; color: #555; }
    .nav a { margin-right: 12px; }
    .updated { font-size: 12px; color: #888; margin-top: 4px; }
    @media (max-width: 600px) { .channel-grid { grid-template-columns: 1fr; }
        .page { padding: 20px 18px 40px; } }
"""


# ============================================================
# HTML base template
# ============================================================

def render_page(title: str, breadcrumb: str, body: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>{html.escape(title)} — Owl Town</title>
  <style>{PAGE_STYLE}</style>
</head>
<body>
  <div class="page">
    <div class="nav">
      <a href="/">Owl Town</a>
      <a href="/channels/">Channels</a>
      <a href="/people/">People</a>
      <a href="/topics.html">Topics</a>
      <a href="/timeline.html">Timeline</a>
      <a href="/sincerity.html">Sincerity</a>
      <a href="/dashboard">Dashboard</a>
    </div>
    <div class="breadcrumb">{breadcrumb}</div>
    <h1>{html.escape(title)}</h1>
{body}
    <p class="updated">Last compiled: {now}</p>
  </div>
</body>
</html>"""


# ============================================================
# Data fetch functions (SQL only, no Gemini)
# ============================================================

def fetch_channel_data(conn: sqlite3.Connection, chat_id: str) -> dict:
    """Return theme, recent case notes, message stats for one channel."""
    row = conn.execute(
        "SELECT theme_text, version, updated_at FROM group_themes WHERE chat_id = ?",
        (chat_id,)
    ).fetchone()
    theme_text, theme_version, theme_updated = row if row else ("", 1, "")

    notes = conn.execute(
        """SELECT note_type, target_username, note_text, created_at
           FROM case_notes WHERE chat_id = ?
           ORDER BY created_at DESC LIMIT 20""",
        (chat_id,)
    ).fetchall()

    msg_7d = conn.execute(
        """SELECT COUNT(*) FROM messages WHERE chat_id = ?
           AND sent_at_utc >= datetime('now', '-7 days')""",
        (chat_id,)
    ).fetchone()[0]

    top_posters = conn.execute(
        """SELECT username, COUNT(*) as cnt FROM messages
           WHERE chat_id = ? AND sent_at_utc >= datetime('now', '-30 days')
           AND username IS NOT NULL
           GROUP BY username ORDER BY cnt DESC LIMIT 5""",
        (chat_id,)
    ).fetchall()

    return {
        "chat_id": chat_id,
        "theme_text": theme_text or "",
        "theme_version": theme_version,
        "theme_updated": theme_updated,
        "notes": notes,
        "msg_7d": msg_7d,
        "top_posters": top_posters,
    }


def fetch_all_profiles(conn: sqlite3.Connection) -> list[dict]:
    """Return all user profiles for users active in any Owl Town chat."""
    chat_ids = list(OWL_TOWN_CHATS.keys())
    placeholders = ",".join("?" * len(chat_ids))
    rows = conn.execute(
        f"""SELECT DISTINCT up.user_id, up.username, up.case_file_text,
                   up.profile_text, up.version, up.updated_at
            FROM user_profiles up
            JOIN messages m ON m.user_id = up.user_id
            WHERE m.chat_id IN ({placeholders})
              AND up.case_file_text IS NOT NULL
            ORDER BY up.username COLLATE NOCASE""",
        chat_ids
    ).fetchall()
    return [
        {
            "user_id": r[0],
            "username": r[1] or f"user_{r[0]}",
            "case_file_text": r[2],
            "profile_text": r[3] or "",
            "version": r[4],
            "updated_at": r[5],
        }
        for r in rows
    ]


def fetch_sincerity_history(conn: sqlite3.Connection) -> dict[str, list]:
    """Return irony score history per username across all Owl Town chats."""
    chat_ids = list(OWL_TOWN_CHATS.keys())
    placeholders = ",".join("?" * len(chat_ids))
    rows = conn.execute(
        f"""SELECT username, week_of, irony_pct, grade
            FROM sincerity_scores
            WHERE chat_id IN ({placeholders})
            ORDER BY username COLLATE NOCASE, week_of""",
        chat_ids
    ).fetchall()
    result: dict[str, list] = {}
    for username, week_of, irony_pct, grade in rows:
        result.setdefault(username, []).append((week_of, irony_pct, grade))
    return result


def fetch_timeline_entries(conn: sqlite3.Connection, limit: int = 60) -> list[tuple]:
    """Return recent case notes across all Owl Town chats for the timeline."""
    chat_ids = list(OWL_TOWN_CHATS.keys())
    placeholders = ",".join("?" * len(chat_ids))
    return conn.execute(
        f"""SELECT chat_id, note_type, target_username, note_text, created_at
            FROM case_notes
            WHERE chat_id IN ({placeholders})
            ORDER BY created_at DESC LIMIT ?""",
        chat_ids + [limit]
    ).fetchall()


# ============================================================
# Gemini compile functions
# ============================================================

def _gemini_call(prompt: str, temperature: float = 0.7) -> str:
    """Make a single Gemini text call. Returns empty string on failure."""
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=temperature),
        )
        return resp.text.strip()
    except Exception as e:
        print(f"[wiki] Gemini call failed: {e}")
        return ""


def compile_channel_article(channel_name: str, theme_text: str, notes: list) -> str:
    """Ask Gemini to write a 2-3 paragraph channel profile article."""
    notes_text = "\n".join(
        f"- [{n[0]}] {n[2]}" for n in notes[:10]
    ) if notes else "No recent observations."

    prompt = f"""You are writing an article for the Owl Town research wiki — a public-facing
document about a group of friends and their chat activity.

Write a 2-3 paragraph profile of the "{channel_name}" channel in an encyclopedic,
dry-but-warm observational voice. Draw only from the information below.
Do not invent facts. Do not use bullet points. Plain prose only.

GROUP THEME:
{theme_text or "(no theme recorded yet)"}

RECENT AGENT OBSERVATIONS:
{notes_text}

Write the article now."""

    return _gemini_call(prompt, temperature=0.8)


def compile_topics_article(conn: sqlite3.Connection) -> list[dict]:
    """Ask Gemini to identify 3-5 recurring cross-channel topics."""
    # Gather a small sample of recent case notes across all channels
    chat_ids = list(OWL_TOWN_CHATS.keys())
    placeholders = ",".join("?" * len(chat_ids))
    notes = conn.execute(
        f"""SELECT chat_id, note_text FROM case_notes
            WHERE chat_id IN ({placeholders})
              AND created_at >= datetime('now', '-30 days')
            ORDER BY created_at DESC LIMIT 60""",
        chat_ids
    ).fetchall()

    if not notes:
        return []

    notes_block = "\n".join(
        f"[{OWL_TOWN_CHATS.get(str(r[0]), r[0])}] {r[1]}"
        for r in notes
    )

    prompt = f"""You are analyzing observations from Owl Town — a group of friends with
six topic-specific chat channels: Omelas Basement (general), Insta(Tele)gram,
Books, AI, Health, and Jocks.

Below are recent agent observations from across all channels.
Identify 3-5 recurring cross-channel topics or themes that come up repeatedly.
For each topic, write a short paragraph (3-5 sentences) in an encyclopedic, dry observational voice.

Format your response exactly like this, with each topic separated by three dashes:
TOPIC: [topic title]
[paragraph text]
---
TOPIC: [next topic title]
[paragraph text]
---

OBSERVATIONS:
{notes_block}

Write the topics now."""

    raw = _gemini_call(prompt, temperature=0.7)
    topics = []
    for block in raw.split("---"):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n", 1)
        if len(lines) == 2 and lines[0].startswith("TOPIC:"):
            title = lines[0].replace("TOPIC:", "").strip()
            body = lines[1].strip()
            slug = title.lower().replace(" ", "-").replace("/", "-")[:40]
            topics.append({"title": title, "slug": slug, "body": body})
    return topics


def compile_index_lede(channel_summaries: list[str]) -> str:
    """Ask Gemini for a short intro paragraph for the index page."""
    combined = "\n".join(f"- {s}" for s in channel_summaries[:6])
    prompt = f"""Write a single short paragraph (2-3 sentences) introducing the Owl Town
research wiki. Owl Town is a constellation of six Telegram group chats monitored
by an AI observational bot. The tone is dry, encyclopedic, quietly fascinated.
Do not mention the bot by name. Do not use the word "journey".

Recent channel activity:
{combined}

Write the intro now."""
    return _gemini_call(prompt, temperature=0.9)


# ============================================================
# HTML render functions
# ============================================================

def linkify_usernames(text: str, known_usernames: set[str]) -> str:
    """Replace known usernames in already-escaped HTML text with links to their people page.
    Matches bare usernames and @username mentions, case-insensitively."""
    import re
    result = text
    for username in sorted(known_usernames, key=len, reverse=True):  # longest first avoids partial matches
        slug = username.lower()
        pattern = re.compile(r'(?<![/@\w])@?' + re.escape(html.escape(username)) + r'(?![\w])', re.IGNORECASE)
        replacement = f'<a href="/people/{slug}.html">{html.escape(username)}</a>'
        result = pattern.sub(replacement, result)
    return result


def _fmt_date(iso: str) -> str:
    try:
        return iso[:10]
    except Exception:
        return iso or ""


def render_note_card(note_type: str, target: str, text: str, created_at: str,
                     known_usernames: set[str] | None = None) -> str:
    if target and known_usernames and target in known_usernames:
        target_str = f" &mdash; <em>re: <a href='/people/{html.escape(target.lower())}.html'>{html.escape(target)}</a></em>"
    elif target:
        target_str = f" &mdash; <em>re: {html.escape(target)}</em>"
    else:
        target_str = ""
    note_html = linkify_usernames(html.escape(text), known_usernames or set())
    return (
        f'<div class="note-card {html.escape(note_type)}">'
        f"{note_html}"
        f'<div class="note-meta">{html.escape(note_type)}{target_str} &middot; {_fmt_date(created_at)}</div>'
        f"</div>"
    )


def _profile_first_line(case_file_text: str | None, max_len: int = 130) -> str:
    """Return the first non-empty line of a case file, truncated."""
    for line in (case_file_text or "").splitlines():
        line = line.strip()
        if line:
            return line[:max_len] + ("…" if len(line) > max_len else "")
    return ""


def _channel_slug(name: str) -> str:
    return name.lower().replace(" ", "-").replace("(", "").replace(")", "")


def render_index(channel_data: list[dict], lede: str,
                 topics: list[dict] | None = None,
                 recent_notes: list[tuple] | None = None,
                 profiles: list[dict] | None = None,
                 known_usernames: set[str] | None = None) -> str:
    ku = known_usernames or set()

    # --- Intro / group theme ---
    if lede:
        intro_html = f"<p>{html.escape(lede)}</p>"
    else:
        home_data = next((cd for cd in channel_data if cd["chat_id"] == OWL_TOWN_HOME), None)
        if home_data and home_data.get("theme_text"):
            first_para = (home_data["theme_text"].split("\n\n")[0]).strip()
            intro_html = f"<p>{linkify_usernames(html.escape(first_para), ku)}</p>"
        else:
            intro_html = ""

    # --- Recent cross-channel topics ---
    topics_html = ""
    if topics:
        items = "".join(
            f"<li><strong><a href='/topics.html'>{html.escape(t['title'])}</a></strong>"
            f" &mdash; {html.escape(t['body'][:120])}{'…' if len(t['body']) > 120 else ''}</li>"
            for t in topics[:5]
        )
        topics_html = f"<h2>Current Topics</h2><ul style='padding-left:24px;margin-bottom:12px'>{items}</ul>"

    # --- Recent field notes (last 6) ---
    notes_html = ""
    if recent_notes:
        cards = "".join(
            render_note_card(n[1], n[2], n[3], n[4], ku)
            for n in recent_notes[:6]
        )
        notes_html = (
            f"<h2>Recent Field Notes</h2>{cards}"
            f"<p style='margin-top:8px;font-size:13px'><a href='/timeline.html'>Full timeline →</a></p>"
        )

    # --- People cards with one-liner ---
    people_html = ""
    if profiles:
        cards = ""
        for p in profiles:
            slug = p["username"].lower()
            one_liner = _profile_first_line(p.get("case_file_text"))
            cards += (
                f'<div class="channel-card" style="padding:10px 14px">'
                f'<h3 style="font-size:15px;margin:0 0 4px">'
                f'<a href="/people/{html.escape(slug)}.html">{html.escape(p["username"])}</a></h3>'
                f'<div class="stat">{html.escape(one_liner)}</div>'
                f'</div>'
            )
        people_html = (
            f"<h2>People</h2>"
            f'<div class="channel-grid">{cards}</div>'
            f"<p style='font-size:13px'><a href='/people/'>All case files →</a></p>"
        )

    # --- Channels as compact footer nav ---
    channel_links = " &middot; ".join(
        f"<a href='/channels/{_channel_slug(OWL_TOWN_CHATS.get(cd['chat_id'], cd['chat_id']))}.html'>"
        f"{html.escape(OWL_TOWN_CHATS.get(cd['chat_id'], cd['chat_id']))}</a>"
        for cd in channel_data
    )
    channels_html = (
        f"<h2>Channels</h2>"
        f"<p>{channel_links} &middot; <a href='/channels/'>index</a></p>"
        f"<p style='font-size:13px;margin-top:8px'>"
        f"<a href='/sincerity.html'>Sincerity Tracker</a></p>"
    )

    body = f"""
{intro_html}
{topics_html}
{notes_html}
{people_html}
{channels_html}"""
    return render_page("Owl Town", "<a href='/'>Owl Town</a>", body)


def render_channel_page(chat_id: str, data: dict, article: str,
                        known_usernames: set[str] | None = None) -> str:
    name = OWL_TOWN_CHATS.get(chat_id, chat_id)
    ku = known_usernames or set()

    def poster_cell(username: str) -> str:
        if username in ku:
            return f'<a href="/people/{html.escape(username.lower())}.html">{html.escape(username)}</a>'
        return html.escape(username)

    posters_rows = "".join(
        f"<tr><td>{poster_cell(p[0])}</td><td>{p[1]}</td></tr>"
        for p in data["top_posters"]
    )

    article_html = ""
    if article:
        article_html = f"<h2>Profile</h2><p>{linkify_usernames(html.escape(article), ku)}</p>"
    elif data["theme_text"]:
        article_html = f"<h2>Group Theme</h2><pre>{linkify_usernames(html.escape(data['theme_text']), ku)}</pre>"

    notes_html = ""
    if data["notes"]:
        notes_html = "<h2>Recent Field Notes</h2>" + "".join(
            render_note_card(n[0], n[1], n[2], n[3], ku) for n in data["notes"]
        )

    posters_html = ""
    if posters_rows:
        posters_html = (
            f'<details style="margin-top:24px"><summary style="cursor:pointer;color:#555;font-size:13px">'
            f'Activity stats</summary>'
            f'<div style="margin-top:8px"><p style="font-size:13px;color:#555">'
            f'{data["msg_7d"]} messages this week</p>'
            f'<table style="margin-top:6px"><tr><th>Username</th><th>Messages (30 days)</th></tr>'
            f'{posters_rows}</table></div></details>'
        )

    body = f"""
{article_html}
{notes_html}
{posters_html}"""

    return render_page(
        name,
        f"<a href='/'>Owl Town</a> &rsaquo; <a href='/channels/'>Channels</a> &rsaquo; {html.escape(name)}",
        body,
    )


def render_channels_index(channel_data: list[dict]) -> str:
    rows = ""
    for cd in channel_data:
        name = OWL_TOWN_CHATS.get(cd["chat_id"], cd["chat_id"])
        slug = name.lower().replace(" ", "-").replace("(", "").replace(")", "")
        rows += f"<tr><td><a href='/channels/{slug}.html'>{html.escape(name)}</a></td><td>{cd['msg_7d']}</td></tr>"

    body = f"""
<table>
<tr><th>Channel</th><th>Messages this week</th></tr>
{rows}
</table>"""
    return render_page(
        "Channels",
        "<a href='/'>Owl Town</a> &rsaquo; Channels",
        body,
    )


def render_people_index(profiles: list[dict]) -> str:
    cards = ""
    for p in profiles:
        slug = p["username"].lower()
        one_liner = _profile_first_line(p.get("case_file_text"), max_len=150)
        cards += (
            f'<div class="channel-card" style="padding:10px 14px">'
            f'<h3 style="font-size:15px;margin:0 0 4px">'
            f'<a href="/people/{html.escape(slug)}.html">{html.escape(p["username"])}</a></h3>'
            f'<div class="stat">{html.escape(one_liner)}</div>'
            f'<div class="updated">v{p["version"]} &middot; {_fmt_date(p["updated_at"])}</div>'
            f'</div>'
        )
    body = f'<div class="channel-grid">{cards}</div>'
    return render_page(
        "People",
        "<a href='/'>Owl Town</a> &rsaquo; People",
        body,
    )


def render_person_page(profile: dict, sincerity_rows: list) -> str:
    username = profile["username"]
    case_file = profile["case_file_text"] or ""
    updated = _fmt_date(profile["updated_at"])

    sincerity_html = ""
    if sincerity_rows:
        rows = "".join(
            f"<tr><td>{r[0]}</td><td>{r[1]:.0f}%</td>"
            f"<td><span class='grade grade-{r[2]}'>{r[2]}</span></td></tr>"
            for r in sincerity_rows[-12:]  # last 12 weeks
        )
        sincerity_html = f"""<h2>Sincerity History</h2>
<table><tr><th>Week</th><th>Irony %</th><th>Grade</th></tr>{rows}</table>"""

    body = f"""
<h2>Case File</h2>
<pre>{html.escape(case_file)}</pre>
<p class="updated">Profile v{profile['version']} &mdash; updated {updated}</p>
{sincerity_html}"""

    return render_page(
        username,
        f"<a href='/'>Owl Town</a> &rsaquo; <a href='/people/'>People</a> &rsaquo; {html.escape(username)}",
        body,
    )


def render_topics_page(topics: list[dict], known_usernames: set[str] | None = None) -> str:
    if not topics:
        body = "<p>No cross-channel topics compiled yet. Check back after the next weekly run.</p>"
        return render_page("Topics", "<a href='/'>Owl Town</a> &rsaquo; Topics", body)

    ku = known_usernames or set()
    sections = ""
    for t in topics:
        sections += f"<h2>{html.escape(t['title'])}</h2><p>{linkify_usernames(html.escape(t['body']), ku)}</p>\n"

    return render_page(
        "Topics",
        "<a href='/'>Owl Town</a> &rsaquo; Topics",
        sections,
    )


def render_timeline(entries: list[tuple], known_usernames: set[str] | None = None) -> str:
    if not entries:
        body = "<p>No field observations recorded yet.</p>"
        return render_page("Timeline", "<a href='/'>Owl Town</a> &rsaquo; Timeline", body)

    ku = known_usernames or set()
    cards = ""
    for chat_id, note_type, target, text, created_at in entries:
        channel = OWL_TOWN_CHATS.get(str(chat_id), str(chat_id))
        if target and target in ku:
            target_str = f" &mdash; <em>re: <a href='/people/{html.escape(target.lower())}.html'>{html.escape(target)}</a></em>"
        elif target:
            target_str = f" &mdash; <em>re: {html.escape(target)}</em>"
        else:
            target_str = ""
        cards += (
            f'<div class="note-card {html.escape(note_type)}">'
            f"{linkify_usernames(html.escape(text), ku)}"
            f'<div class="note-meta">{html.escape(channel)} &middot; '
            f"{html.escape(note_type)}{target_str} &middot; {_fmt_date(created_at)}</div>"
            f"</div>\n"
        )

    return render_page(
        "Timeline",
        "<a href='/'>Owl Town</a> &rsaquo; Timeline",
        cards,
    )


def render_sincerity_page(history: dict[str, list]) -> str:
    if not history:
        body = "<p>No sincerity scores recorded yet.</p>"
        return render_page("Sincerity Tracker", "<a href='/'>Owl Town</a> &rsaquo; Sincerity", body)

    # Collect all weeks seen
    all_weeks: list[str] = sorted({
        entry[0] for entries in history.values() for entry in entries
    })[-12:]  # last 12 weeks

    header = "<tr><th>Subject</th>" + "".join(f"<th>{w}</th>" for w in all_weeks) + "</tr>"
    rows = ""
    for username in sorted(history.keys(), key=str.lower):
        week_map = {e[0]: e[2] for e in history[username]}  # week -> grade
        cells = "".join(
            f"<td><span class='grade grade-{week_map[w]}'>{week_map[w]}</span></td>"
            if w in week_map else "<td>&mdash;</td>"
            for w in all_weeks
        )
        rows += f"<tr><td><a href='/people/{html.escape(username.lower())}.html'>{html.escape(username)}</a></td>{cells}</tr>"

    body = f"""
<p>Weekly DFW Sincerity Index grades for Owl Town members.
A = Earnest, B = Mostly Sincere, C = Balanced, D = Leaning Ironic, F = Fully Ironic.</p>
<div style="overflow-x:auto">
<table><thead>{header}</thead><tbody>{rows}</tbody></table>
</div>"""

    return render_page(
        "Sincerity Tracker",
        "<a href='/'>Owl Town</a> &rsaquo; Sincerity",
        body,
    )


# ============================================================
# File writer
# ============================================================

def write_page(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ============================================================
# Main orchestration
# ============================================================

def build_wiki(gemini_enabled: bool = True) -> int:
    """Build the full wiki. Returns number of pages written."""
    if not DB_PATH.exists():
        print(f"[wiki] DB not found at {DB_PATH}")
        return 0

    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    pages = 0

    conn = sqlite3.connect(DB_PATH)
    try:
        # --- Fetch all data ---
        channel_data = [fetch_channel_data(conn, cid) for cid in OWL_TOWN_CHATS]
        profiles = fetch_all_profiles(conn)
        sincerity = fetch_sincerity_history(conn)
        timeline_entries = fetch_timeline_entries(conn)

        # --- Gemini compile (optional) ---
        channel_articles: dict[str, str] = {}
        topics: list[dict] = []
        lede = ""

        if gemini_enabled and GEMINI_API_KEY:
            print("[wiki] Compiling channel articles...")
            for cd in channel_data:
                name = OWL_TOWN_CHATS.get(cd["chat_id"], cd["chat_id"])
                article = compile_channel_article(name, cd["theme_text"], cd["notes"])
                channel_articles[cd["chat_id"]] = article

            print("[wiki] Compiling cross-channel topics...")
            topics = compile_topics_article(conn)

            summaries = [
                f"{OWL_TOWN_CHATS.get(cd['chat_id'], cd['chat_id'])}: {cd['msg_7d']} messages this week"
                for cd in channel_data
            ]
            print("[wiki] Compiling index lede...")
            lede = compile_index_lede(summaries)
        else:
            print("[wiki] Gemini disabled — building data-only wiki.")

    finally:
        conn.close()

    # Build the set of known usernames for backlink resolution
    known_usernames: set[str] = {p["username"] for p in profiles if p["username"]}

    # --- Render and write pages ---

    # Index
    write_page(WIKI_DIR / "index.html", render_index(
        channel_data, lede,
        topics=topics,
        recent_notes=timeline_entries[:7],
        profiles=profiles,
        known_usernames=known_usernames,
    ))
    pages += 1

    # Channels index
    write_page(WIKI_DIR / "channels" / "index.html", render_channels_index(channel_data))
    pages += 1

    # Per-channel pages
    for cd in channel_data:
        name = OWL_TOWN_CHATS.get(cd["chat_id"], cd["chat_id"])
        slug = name.lower().replace(" ", "-").replace("(", "").replace(")", "")
        article = channel_articles.get(cd["chat_id"], "")
        write_page(
            WIKI_DIR / "channels" / f"{slug}.html",
            render_channel_page(cd["chat_id"], cd, article, known_usernames),
        )
        pages += 1

    # People index
    write_page(WIKI_DIR / "people" / "index.html", render_people_index(profiles))
    pages += 1

    # Per-person pages
    for profile in profiles:
        slug = profile["username"].lower()
        user_sincerity = sincerity.get(profile["username"], [])
        write_page(
            WIKI_DIR / "people" / f"{slug}.html",
            render_person_page(profile, user_sincerity),
        )
        pages += 1

    # Topics
    write_page(WIKI_DIR / "topics.html", render_topics_page(topics, known_usernames))
    pages += 1

    # Timeline
    write_page(WIKI_DIR / "timeline.html", render_timeline(timeline_entries, known_usernames))
    pages += 1

    # Sincerity
    write_page(WIKI_DIR / "sincerity.html", render_sincerity_page(sincerity))
    pages += 1

    print(f"[wiki] Done — {pages} pages written to {WIKI_DIR}")
    return pages


if __name__ == "__main__":
    gemini_on = "--no-gemini" not in sys.argv
    build_wiki(gemini_enabled=gemini_on)
