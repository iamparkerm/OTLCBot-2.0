# OTLCBot-2.0
Trying again in 2026

# OTLCBot

A lightweight Telegram bot that logs chat messages to SQLite and sends a **weekly summary** to a Telegram group.  
Designed to run unattended on a Raspberry Pi Zero W.

---

## What it does

- Logs Telegram chat messages to a SQLite database
- Sends a **weekly summary** including:
  - total messages
  - top posters
- (Optional) Generates an AI-written weekly recap (currently disabled)
- Backs up the database weekly
- Prunes old messages monthly
- Runs automatically via `cron`

---

## Project structure

OTLCBot-2.0/
├── src/
│ ├── weekly.py # Weekly summary sender
│ ├── prune_db.py # Monthly DB cleanup
│ └── ...
├── scripts/
│ └── backup_db.sh # DB backup script
├── .env # Secrets & config (NOT committed)
├── .env.example # Template for required env vars
├── .gitignore
└── README.md


---
## Setup
### 1. Clone the repo
git clone <your-github-repo-url>
cd OTLCBot-2.0
2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
3. Configure environment variables
Copy the template and fill in values:

cp .env.example .env
nano .env
Required:

TELEGRAM_BOT_TOKEN

TELEGRAM_CHAT_ID

DB_PATH (default: /opt/otlc/data.db)

Optional:

OPENAI_API_KEY

ENABLE_AI_SUMMARY

Running manually
source .venv/bin/activate
python src/weekly.py
Cron jobs
This project is intended to run automatically via cron.

Example cron entries:

# Weekly summary (Sunday 6pm)
0 18 * * 0 /home/parker/OTLCBot-2.0/.venv/bin/python /home/parker/OTLCBot-2.0/src/weekly.py >> /home/parker/OTLCBot-2.0/weekly.log 2>&1

# Weekly DB backup
15 18 * * 0 /home/parker/OTLCBot-2.0/scripts/backup_db.sh

# Monthly DB prune (1st of month)
40 3 1 * * /home/parker/OTLCBot-2.0/.venv/bin/python /home/parker/OTLCBot-2.0/src/prune_db.py >> /home/parker/OTLCBot-2.0/prune.log 2>&1
Notes
.env is intentionally ignored by Git — do not commit secrets.

SQLite DB is external to the repo (/opt/otlc/data.db).

AI summaries are optional and disabled by default.

Designed for low-resource hardware (Raspberry Pi Zero).
