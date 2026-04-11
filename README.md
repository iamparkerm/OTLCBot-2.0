# OTLCBot 2.0

An AI-powered Telegram observer bot that watches group chats, builds rolling personality profiles, sends weekly illustrated reports, and maintains a public research wiki. Runs unattended on a Raspberry Pi Zero W.

---

## What it does

- **Logs** all Telegram messages to SQLite
- **Observes** group dynamics via an autonomous agent loop (Observe → Reason → Act)
- **Profiles** members with rolling Gemini-generated case files, updated weekly
- **Scores** irony vs. sincerity weekly (DFW Sincerity Index)
- **Illustrates** each week with a generated New Yorker-style cartoon
- **Publishes** a static research wiki at `wiki.otlconline.net`
- **Tracks** bets, watchlists, and group themes across channels
- **Reports** split by group: Owl Town (6 topic channels) and Penetr8in

---

## Architecture

```
src/
├── config.py       # All constants and env vars (single source of truth)
├── bot.py          # Telegram bot: message logging, commands, agent trigger
├── observer.py     # Observer agent: reads messages → writes case_notes (every 2h, no public output)
├── agent.py        # Speaker agent: reads case_notes → reasons → posts publicly (every 3h)
├── profiles.py     # Group themes, user profiles, case files, DB bootstrap
├── sincerity.py    # DFW sincerity pipeline: scoring, grading, trend tracking
├── reports.py      # Conversation windows, AI recap, gazette, image generation
├── weekly.py       # Orchestrator: runs Friday pipeline for OT and Penetr8in
├── wiki.py         # Static wiki compiler: builds HTML from DB to /opt/otlc/wiki/
├── webapp.py       # Flask server: serves wiki + Telegram MiniApp dashboard
└── prune_db.py     # Monthly DB cleanup

scripts/
├── backup_db.sh        # Weekly DB backup
└── start-tunnel.sh     # Cloudflare named tunnel (wiki.otlconline.net)
```

### Two-agent architecture

The bot uses a split Observer/Speaker model:

- **Observer** (`observer.py`) — runs every 2 hours, reads recent messages, writes internal `case_notes`, updates group themes. Never sends Telegram messages. No bot object required.
- **Speaker** (`agent.py`) — runs every 3 hours (and on message-count threshold in bot.py), reads `case_notes` as context, reasons about what to post, executes public tools. All outbound messages go through here.

This separation means the bot accumulates observations continuously without over-posting. The Speaker reads the Observer's case notes as grounding for its decisions.

### Module dependency flow

```
config  ←  profiles, sincerity, reports
profiles  ←  reports (get_group_theme for grounding)
profiles, sincerity, reports  ←  weekly (orchestrator), agent, observer
```

No module imports from `weekly.py`. `observer.py` and `agent.py` are standalone scripts.

---

## Group structure

**Owl Town** — 6 topic-specific channels aggregated into one combined weekly report:
- Omelas Basement (home, receives the combined report)
- Insta(Tele)gram, Books, AI, Health, Jocks

**Penetr8in** — standalone group, gets its own weekly report + agent

---

## Setup

### 1. Clone and install

```bash
git clone <your-github-repo-url>
cd OTLCBot-2.0
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
nano .env
```

Required:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID` — comma-separated chat IDs for standalone reports
- `DB_PATH` — default: `/opt/otlc/data.db`

Optional:
- `GEMINI_API_KEY` — enables AI recap, profiles, sincerity, images, wiki
- `ENABLE_AI_SUMMARY=true`
- `ENABLE_SINCERITY_INDEX=true`
- `ENABLE_AGENT=true`
- `OWL_TOWN_CHAT_IDS` — comma-separated Owl Town chat IDs
- `OWL_TOWN_SEND_TO` — chat ID to receive the combined OT report
- `OWL_TOWN_NAMES` — `chatid=Name,chatid=Name` friendly name map
- `WIKI_DIR` — default: `/opt/otlc/wiki`
- `WEBAPP_URL` — dashboard deep-link (e.g. `https://wiki.otlconline.net/dashboard?chat_id=...`)

### 3. Run the bot

```bash
source .venv/bin/activate
python src/bot.py
```

### 4. Serve the wiki (optional)

```bash
python src/webapp.py          # Flask on port 5000
bash scripts/start-tunnel.sh  # Cloudflare tunnel → wiki.otlconline.net
```

---

## Bot commands

| Command | Description |
|---------|-------------|
| `/start` | Check if bot is running |
| `/chatid` | Get the current chat ID |
| `/stats` | Top posters in the last 24h |
| `/bet` | Create a new bet |
| `/bets` | List all open bets |
| `/settlebet <id> <winner>` | Settle a bet |
| `/watch <title>` | Add a movie/show to the watchlist |
| `/read <title>` | Add a book to the watchlist |

---

## Cron jobs (Pi)

```
# Observer: read messages, write case_notes (every 2 hours)
0 */2 * * * /home/parker/OTLCBot-2.0/.venv/bin/python /home/parker/OTLCBot-2.0/src/observer.py >> observer.log 2>&1

# Speaker: reason + post publicly (every 3 hours)
0 */3 * * * /home/parker/OTLCBot-2.0/.venv/bin/python /home/parker/OTLCBot-2.0/src/agent.py >> agent.log 2>&1

# Owl Town weekly report (Friday 3pm EST)
0 15 * * 5 /home/parker/OTLCBot-2.0/.venv/bin/python /home/parker/OTLCBot-2.0/src/weekly.py --group owltown >> weekly.log 2>&1

# Penetr8in weekly report (Friday 4pm EST) + cost DM + wiki rebuild
0 16 * * 5 /home/parker/OTLCBot-2.0/.venv/bin/python /home/parker/OTLCBot-2.0/src/weekly.py --group penetr8in >> weekly.log 2>&1

# Weekly DB backup (Sunday 6:15pm)
15 18 * * 0 /home/parker/OTLCBot-2.0/scripts/backup_db.sh

# Monthly DB prune (1st of month, 3:40am)
40 3 1 * * /home/parker/OTLCBot-2.0/.venv/bin/python /home/parker/OTLCBot-2.0/src/prune_db.py >> otlc_prune.log 2>&1
```

---

## Notes

- `.env` is intentionally ignored by Git — do not commit secrets.
- SQLite DB lives outside the repo at `/opt/otlc/data.db`.
- Wiki static files are written to `/opt/otlc/wiki/` and served by Flask.
- AI features are all optional — the bot runs fine without a Gemini key.
- Designed for low-resource hardware (Raspberry Pi Zero W, 512MB RAM).
