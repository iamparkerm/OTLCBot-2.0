"""
OTLCBot WebApp — minimal Flask server for the Telegram MiniApp gallery.

Run locally:  python src/webapp.py
Expose via:   cloudflared tunnel --url http://localhost:5000
Then set:     WEBAPP_URL=https://<your-tunnel>.trycloudflare.com  in .env
"""
import os
import sqlite3
import requests
from pathlib import Path

from flask import Flask, jsonify, send_from_directory, abort
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=ROOT / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", str(ROOT / "data.db"))
SRC_DIR = Path(__file__).parent

app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(SRC_DIR, "gallery.html")


@app.route("/api/images/<int:chat_id>")
def api_images(chat_id: int):
    """Return JSON list of gallery images for a chat, newest first."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT week_of, image_prompt, telegram_file_id
                FROM weekly_images
                WHERE chat_id = ?
                ORDER BY week_of ASC, id ASC;
                """,
                (chat_id,),
            ).fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = []
    for week_of, prompt, file_id in rows:
        url = _resolve_file_url(file_id)
        result.append({"week_of": week_of, "prompt": prompt or "", "url": url})

    # Return newest-first for the UI
    result.reverse()
    return jsonify(result)


def _resolve_file_url(file_id: str) -> str | None:
    """Call Telegram getFile to turn a file_id into a downloadable URL."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=6,
        )
        if r.ok:
            path = r.json()["result"]["file_path"]
            return f"https://api.telegram.org/file/bot{TOKEN}/{path}"
    except Exception:
        pass
    return None


if __name__ == "__main__":
    port = int(os.getenv("WEBAPP_PORT", "5000"))
    print(f"Starting OTLCBot WebApp on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
