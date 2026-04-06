"""
OTLCBot WebApp — Flask server for the Telegram MiniApp gallery.

Run locally:  python src/webapp.py
Expose via:   cloudflared tunnel --url http://localhost:5000
Then set:     WEBAPP_URL=https://<your-tunnel>.trycloudflare.com  in .env
"""
import os
import sqlite3
import requests
from pathlib import Path

from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory, Response, abort
from dotenv import load_dotenv
from werkzeug.routing import BaseConverter

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=ROOT / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", str(ROOT / "data.db"))
SRC_DIR = Path(__file__).parent
WIKI_DIR = Path(os.getenv("WIKI_DIR", "/opt/otlc/wiki"))

app = Flask(__name__)


class SignedIntConverter(BaseConverter):
    """URL converter that handles negative integers (for Telegram chat IDs)."""
    regex = r"-?\d+"

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(value)


app.url_map.converters["signed_int"] = SignedIntConverter

# In-memory cache: telegram file_id -> (file_path, fetched_at)
_file_path_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 3600  # Telegram file paths are valid ~1 hour


@app.route("/")
def wiki_index():
    if WIKI_DIR.exists():
        return send_from_directory(WIKI_DIR, "index.html")
    return send_from_directory(SRC_DIR, "gallery.html")


@app.route("/wiki/<path:filename>")
def wiki_static(filename):
    if not WIKI_DIR.exists():
        abort(404)
    return send_from_directory(WIKI_DIR, filename)


@app.route("/<path:filename>")
def wiki_catch_all(filename):
    """Serve any path from WIKI_DIR, handling both files and directory index pages."""
    if not WIKI_DIR.exists():
        abort(404)
    target = WIKI_DIR / filename
    if target.is_file():
        return send_from_directory(WIKI_DIR, filename)
    # Try as a directory index
    index = target / "index.html"
    if index.is_file():
        return send_from_directory(WIKI_DIR, str(Path(filename) / "index.html"))
    abort(404)


@app.route("/dashboard")
def dashboard_app():
    return send_from_directory(SRC_DIR, "gallery.html")


@app.route("/robots.txt")
def robots_txt():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.route("/api/images/<signed_int:chat_id>")
def api_images(chat_id: int):
    """Return JSON list of gallery images for a chat, newest first."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT id, week_of, image_prompt
                FROM weekly_images
                WHERE chat_id = ?
                ORDER BY week_of DESC, id DESC;
                """,
                (chat_id,),
            ).fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = []
    for row_id, week_of, prompt in rows:
        result.append({
            "id": row_id,
            "week_of": week_of,
            "prompt": prompt or "",
            "url": f"/img/{row_id}",
        })

    return jsonify(result)


@app.route("/api/profiles/<signed_int:chat_id>")
def api_profiles(chat_id: int):
    """Return user case file profiles for users active in this chat."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT up.user_id, up.username, up.case_file_text,
                       up.version, up.updated_at
                FROM user_profiles up
                JOIN messages m ON m.user_id = up.user_id
                WHERE m.chat_id = ?
                  AND up.case_file_text IS NOT NULL
                ORDER BY up.username COLLATE NOCASE;
                """,
                (chat_id,),
            ).fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = []
    for user_id, username, case_file_text, version, updated_at in rows:
        result.append({
            "user_id": user_id,
            "username": username or f"User {user_id}",
            "case_file": case_file_text,
            "version": version,
            "updated_at": updated_at,
        })

    return jsonify(result)


@app.route("/api/bets/<signed_int:chat_id>")
def api_bets(chat_id: int):
    """Return all bets for a chat, newest first."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT id, description, settlement, wager,
                       created_by_name, created_at, settled_at, winner
                FROM bets
                WHERE chat_id = ?
                ORDER BY created_at DESC;
                """,
                (chat_id,),
            ).fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = []
    for row_id, desc, settlement, wager, creator, created, settled, winner in rows:
        result.append({
            "id": row_id,
            "description": desc,
            "settlement": settlement,
            "wager": wager,
            "created_by_name": creator or "Unknown",
            "created_at": created,
            "settled_at": settled,
            "winner": winner,
        })
    return jsonify(result)


ALLOWED_MEDIA_TYPES = {"Movie", "Book", "Show", "Podcast", "Article", "Other"}


@app.route("/api/watchlist/<signed_int:chat_id>", methods=["GET"])
def api_watchlist(chat_id: int):
    """Return watchlist items with completion info."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            items = conn.execute(
                """
                SELECT id, title, media_type, added_by_id, added_by_username, added_at
                FROM watchlist
                WHERE chat_id = ?
                ORDER BY added_at DESC;
                """,
                (chat_id,),
            ).fetchall()

            if not items:
                return jsonify([])

            item_ids = [row[0] for row in items]
            placeholders = ",".join("?" * len(item_ids))
            completions = conn.execute(
                f"""
                SELECT item_id, user_id, completed_at, rating
                FROM watchlist_completions
                WHERE item_id IN ({placeholders});
                """,
                item_ids,
            ).fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    comp_map: dict[int, list] = {}
    for item_id, user_id, completed_at, rating in completions:
        comp_map.setdefault(item_id, []).append({
            "user_id": user_id,
            "completed_at": completed_at,
            "rating": rating,
        })

    result = []
    for row_id, title, media_type, added_by_id, added_by_username, added_at in items:
        result.append({
            "id": row_id,
            "title": title,
            "media_type": media_type,
            "added_by_id": added_by_id,
            "added_by_username": added_by_username or f"User {added_by_id}",
            "added_at": added_at,
            "completed_by": comp_map.get(row_id, []),
        })
    return jsonify(result)


@app.route("/api/watchlist/<signed_int:chat_id>", methods=["POST"])
def api_watchlist_add(chat_id: int):
    """Add a new item to the watchlist."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    media_type = data.get("media_type", "Other")
    user_id = data.get("user_id")
    username = data.get("username")

    if not title:
        return jsonify({"error": "Title is required"}), 400
    if media_type not in ALLOWED_MEDIA_TYPES:
        return jsonify({"error": f"Invalid media_type. Must be one of: {', '.join(sorted(ALLOWED_MEDIA_TYPES))}"}), 400
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                """
                INSERT INTO watchlist (chat_id, title, media_type, added_by_id, added_by_username, added_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (chat_id, title, media_type, user_id, username, now),
            )
            new_id = cur.lastrowid
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "id": new_id,
        "title": title,
        "media_type": media_type,
        "added_by_id": user_id,
        "added_by_username": username or f"User {user_id}",
        "added_at": now,
        "completed_by": [],
    }), 201


@app.route("/api/watchlist/<signed_int:chat_id>/complete", methods=["POST"])
def api_watchlist_complete(chat_id: int):
    """Mark a watchlist item as completed by a user, with optional star rating."""
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")
    user_id = data.get("user_id")
    rating = data.get("rating")

    if not item_id or not user_id:
        return jsonify({"error": "item_id and user_id are required"}), 400
    if rating is not None and (not isinstance(rating, int) or rating < 1 or rating > 5):
        return jsonify({"error": "rating must be 1-5"}), 400

    try:
        with sqlite3.connect(DB_PATH) as conn:
            # Verify item belongs to this chat
            row = conn.execute(
                "SELECT id FROM watchlist WHERE id = ? AND chat_id = ?;",
                (item_id, chat_id),
            ).fetchone()
            if not row:
                return jsonify({"error": "Item not found in this chat"}), 404

            now = datetime.now(timezone.utc).isoformat()
            # Use INSERT OR REPLACE so rating can be updated
            conn.execute(
                """
                INSERT INTO watchlist_completions (item_id, user_id, completed_at, rating)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(item_id, user_id) DO UPDATE SET rating = excluded.rating, completed_at = excluded.completed_at;
                """,
                (item_id, user_id, now, rating),
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


@app.route("/api/case-notes/<signed_int:chat_id>")
def api_case_notes(chat_id: int):
    """Return recent case notes for a chat, newest first."""
    limit = request.args.get("limit", 50, type=int)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT id, note_type, target_username, note_text, created_at
                FROM case_notes
                WHERE chat_id = ?
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                (chat_id, limit),
            ).fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = []
    for row_id, note_type, target, text, created_at in rows:
        result.append({
            "id": row_id,
            "note_type": note_type,
            "target_username": target,
            "note_text": text,
            "created_at": created_at,
        })
    return jsonify(result)


@app.route("/img/<int:image_id>")
def proxy_image(image_id: int):
    """Proxy a weekly image by its DB id — keeps the bot token server-side."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT telegram_file_id FROM weekly_images WHERE id = ?;",
                (image_id,),
            ).fetchone()
    except Exception:
        return Response("DB error", status=500)

    if not row:
        return Response("Not found", status=404)

    file_id = row[0]
    file_path = _get_telegram_file_path(file_id)
    if not file_path:
        return Response("Could not resolve image", status=502)

    # Stream the image from Telegram's servers
    try:
        r = requests.get(
            f"https://api.telegram.org/file/bot{TOKEN}/{file_path}",
            timeout=15,
            stream=True,
        )
        if not r.ok:
            return Response("Upstream error", status=502)

        content_type = r.headers.get("Content-Type", "image/jpeg")
        return Response(
            r.iter_content(chunk_size=8192),
            content_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except Exception:
        return Response("Failed to fetch image", status=502)


def _get_telegram_file_path(file_id: str) -> str | None:
    """Resolve a Telegram file_id to a file_path, with caching."""
    import time

    cached = _file_path_cache.get(file_id)
    if cached:
        path, fetched_at = cached
        if time.time() - fetched_at < _CACHE_TTL:
            return path

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=6,
        )
        if r.ok:
            path = r.json()["result"]["file_path"]
            _file_path_cache[file_id] = (path, time.time())
            return path
    except Exception:
        pass
    return None


if __name__ == "__main__":
    port = int(os.getenv("WEBAPP_PORT", "5000"))
    print(f"Starting OTLCBot WebApp on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
