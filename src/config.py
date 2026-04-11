"""
Central configuration for OTLCBot.

All modules import constants from here — no module re-reads .env independently
(except the load_dotenv call below which is safe to repeat).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=ROOT / ".env")

# ---------- Database ----------
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data.db")).expanduser().resolve()

# ---------- Telegram ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [cid.strip() for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]

# ---------- Gemini ----------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ENABLE_AI_SUMMARY = os.getenv("ENABLE_AI_SUMMARY", "false").lower() == "true"
ENABLE_SINCERITY_INDEX = os.getenv("ENABLE_SINCERITY_INDEX", "false").lower() == "true"
SINCERITY_SNIPPET_LIMIT = int(os.getenv("SINCERITY_SNIPPET_LIMIT", "50"))
ENABLE_AGENT = os.getenv("ENABLE_AGENT", "false").lower() == "true"

# ---------- Owl Town group structure ----------
#
# We manage two independent groups:
#
# 1. Penetr8in' Experiences  (chat_id: -1003792615572)
#    Standalone group — own weekly report + agent.
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

OWL_TOWN_CHAT_IDS = [
    cid.strip() for cid in os.getenv("OWL_TOWN_CHAT_IDS", "").split(",") if cid.strip()
]
OWL_TOWN_SEND_TO = os.getenv("OWL_TOWN_SEND_TO", "")
OWL_TOWN_NAMES: dict[str, str] = {}
for _pair in os.getenv("OWL_TOWN_NAMES", "").split(","):
    if "=" in _pair:
        _cid, _name = _pair.split("=", 1)
        OWL_TOWN_NAMES[_cid.strip()] = _name.strip()

# ---------- Admin ----------
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "KarlPopper")

# ---------- Gemini pricing ----------
COST_PER_IMAGE = 0.039        # gemini-2.5-flash-image, per image
COST_PER_TEXT_CALL = 0.0015   # gemini-2.5-flash-lite, rough average per call

# ---------- Bot persona ----------
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
