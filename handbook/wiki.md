# OTLCBot

**OTLCBot** is a Telegram bot built on a Raspberry Pi Zero W that performs long-term observation of group chat activity. It logs every message, generates AI-powered weekly reports, maintains rolling personality profiles of its subjects, and runs an autonomous agent that occasionally interjects with dry, observational commentary. The bot speaks in the voice of a not-quite-human intelligence conducting field research on a group of friends — a persona inspired by David Foster Wallace's critique of irony and sincerity. It has been in continuous operation since early 2026.

## Overview

OTLCBot was built as a personal project to add a layer of structured observation to a small network of friend-group Telegram chats. It combines message logging, social features (group betting, shared watchlists), and AI-generated content through Google Gemini into a system that watches, remembers, and — on occasion — speaks.

The bot monitors two deployment contexts. The first is a standalone group chat. The second is **Owl Town**, a constellation of six topic-specific chats (books, sports, health, AI, media, and a general "basement") whose activity is aggregated into a single combined weekly report. All AI-generated content — recaps, cartoons, personality profiles, sincerity assessments — passes through a consistent persona that treats the group members as subjects in an ongoing study. See [[Architecture]] for the full system design.

## Commands

| Command | Category | Description |
|---------|----------|-------------|
| `/start` | Utility | Confirms the bot is running |
| `/chatid` | Utility | Returns the current chat's Telegram ID |
| `/stats` | Utility | Top 10 posters in the last 24 hours |
| `/help` | Utility | Lists all available commands |
| `/bet` | Betting | Creates a new bet (guided 3-step conversation: description, settlement criteria, wager) |
| `/bets` | Betting | Lists all open bets in the chat |
| `/settlebet` | Betting | Settles a bet with a declared winner |
| `/watch` | Media | Adds a movie or show to the group watchlist |
| `/read` | Media | Adds a book to the group watchlist |
| `/watchlist` | Media | Shows all watchlist items |
| `/rate` | Media | Rates a watchlist item (1-5 stars) |
| `/gallery` | Navigation | Browse weekly AI-generated cartoons in-chat |
| `/dashboard` | Navigation | Opens the full Telegram MiniApp dashboard |

## Agent System

The agent system is split into two separate processes that run on independent schedules. See [[Agent System]] for the full architecture.

**Observer** (`observer.py`) runs every 4 hours. It reads recent messages silently, asks Gemini to generate internal case notes, and writes them to the `case_notes` table. It never posts to any chat. When new notes are filed, it also triggers a wiki rebuild so People, Timeline, and Sincerity pages stay current between Friday runs.

**Speaker** (`agent.py`) runs every 3 hours (and also after a configurable message-count threshold is crossed in bot.py). It reads the group's current state — including the Observer's accumulated case notes — and asks Gemini whether to act publicly. Most of the time it chooses silence.

The Speaker has five tools:

| Tool | What it does |
|------|-------------|
| `send_commentary` | Posts a brief field observation about recent conversation |
| `illustrated_summary` | Generates an AI cartoon of the week's activity with a caption |
| `sincerity_check` | Runs the DFW Sincerity Index and shares group-level results |
| `add_media` | Extracts an organic media recommendation from chat and adds it to the watchlist |
| `update_casefile` | Identifies a personality-revealing moment, updates a subject's profile, and announces the discovery |

Tools self-register via a decorator pattern. The Speaker enforces the following rules before acting: a 4-hour cooldown since the last public action; at least 4 user messages posted since that action (prevents the bot commenting on its own output); at least 5 messages in the last 24 hours; and each tool may only fire once per run across all chats (preventing the same action from hitting multiple threads in a single sweep). The admin user is excluded from commentary targets.

## Weekly Reports

Every Friday at 3pm EST, a cron job triggers the full weekly reporting pipeline. For each monitored chat, the bot assembles a report containing message counts, the most active participants, and an AI-generated field-notes recap based on representative conversation excerpts selected through burst detection — an algorithm that groups temporally proximate messages into coherent conversation windows rather than sampling at random. See [[Weekly Reports]] for the complete pipeline.

The report includes a **DFW Sincerity Index**: a Gemini-powered assessment of the group's irony-to-sincerity ratio, scored as a percentage and converted to a letter grade (A through F). Each user receives a private DM with their individual score and a trend arrow comparing to the previous week. The group sees only the aggregate.

For Owl Town, the bot produces a combined report aggregating all six sub-chats, accompanied by a prose **gazette** — a ~200-word briefing written in the bot's observational voice, summarizing the week's activity as a field report. A weekly AI-generated cartoon is also produced and posted alongside the report.

An admin cost DM closes the cycle, reporting Gemini API usage, estimated monthly costs, and Raspberry Pi system health (CPU temperature, memory, disk, uptime).

## Memory System

The bot maintains a three-tier memory architecture. See [[Memory System]] for details.

**Tier 1 — Case Notes.** Short-term observations written by the Observer every 4 hours. For each active chat the Observer generates 1–3 notes tagged by type (`observation`, `discovery`) and optional target user, then stores them in the `case_notes` table. The Speaker also writes notes when it posts publicly (`commentary`) or updates a case file (`discovery`). Notes accumulate between Friday runs and give the Speaker continuity — without them, each evaluation would be amnesiac.

**Tier 2 — Profiles and Themes.** Long-term consolidated memory. Every Friday, Gemini merges each user's raw messages with discovery notes from the past two weeks into a rolling **user profile** — a personality summary tracking recurring topics, interests, communication style, and humor patterns. A parallel **group theme** profile captures the chat's culture: running jokes, shared references, dynamics. The Observer also refreshes group themes mid-week when a chat generates 20+ messages in a 4-hour window. Both profile types consolidate rather than append, dropping stale details that have not recurred.

**Tier 3 — Execution Log.** The `agent_actions` table records every public action the Speaker takes — what it chose, why, and whether it succeeded. Used for cooldown enforcement. Quiet `nothing` decisions are intentionally not logged, so the cooldown clock only resets when the bot actually speaks.

Case notes flow upward into weekly profile updates, so the long-term memory absorbs what the Observer noticed between reports. The Speaker reads the five most recent case notes before each decision cycle.

User profiles are versioned, and at milestones (2, 4, 8, 13, 26 weeks of observation), the bot announces the occasion to the group.

## Dashboard

The `/dashboard` command opens a Telegram MiniApp — a single-page web application served by a Flask backend and exposed via a Cloudflare tunnel. It provides five tabs: **Gallery** (weekly AI cartoons with prompt text), **User Profiles** (case file dossiers in monospace), **Bets** (open and settled, color-coded), **Watch/Read** (shared media list with star ratings), and **Field Notes** (a timeline of the bot's observations). See [[Dashboard]] for the technical implementation.

## Infrastructure

OTLCBot runs on a Raspberry Pi Zero W with three systemd services: the Telegram bot, the Flask webapp, and a Cloudflare named tunnel serving `wiki.otlconline.net`. Five cron jobs run on schedule: the Observer (every 4 hours), the Speaker (every 3 hours), the Friday weekly report (OT at 3pm EST, Penetr8in at 4pm EST), a weekly database backup, and a monthly message prune retaining one year of history. All data lives in a single SQLite database. Configuration is entirely `.env`-driven, with toggleable features for AI summaries, sincerity scoring, and the agent layer. See [[Infrastructure]] for the full setup.

## Personality

The bot's voice is defined by a single `BOT_PERSONA` constant injected into every Gemini prompt across the system. It describes the bot as an AI that has been quietly observing a group chat full of humans for weeks — finding them confusing, sentimental, contradictory, and, in Wallace's words, "unavoidably naive and goo-prone." The tone is dry, observational, wry, never mean, and quietly fascinated by the gap between what humans say they believe and what they actually do. It speaks like something not-quite-human filing a field report that happens to be accidentally poetic.

This voice is consistent across weekly recaps, agent commentary, case file dossiers, sincerity assessments, image captions, milestone announcements, and the Owl Town gazette. The headers and structural elements remain understated — the personality comes through in the generated prose, not the formatting.

## See also

- [[Architecture]] — System design and data flow
- [[Agent System]] — Autonomous decision layer, tool registry, reasoning loop
- [[Weekly Reports]] — Friday pipeline, sincerity scoring, image generation, gazette
- [[Memory System]] — Three-tier observation model, consolidation, grounding
- [[Dashboard]] — Telegram MiniApp, Flask API, gallery interface
- [[Infrastructure]] — Pi deployment, systemd services, cron, schema, configuration
