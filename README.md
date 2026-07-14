# Romano transfer bot

Telegram bot + **web app** that forward **confirmed transfers, "HERE WE GO"
calls, and big-club interest** from top transfer journalists. Polls a free news
API on a schedule; no server to run.

**App:** https://hasayes.github.io/romano-bot/ — installable PWA (iPhone:
Share → Add to Home Screen) with the full card feed, club/stage filters,
per-player timelines, and Web Push notifications. Served from `docs/` by
GitHub Pages; the poller commits each card to `docs/feed.json` and pushes to
subscriptions stored in the `PUSH_SUBSCRIPTIONS` Actions secret (VAPID key in
`VAPID_PRIVATE_KEY_PEM`).

Follows: **Fabrizio Romano, David Ornstein, Gianluca Di Marzio, Matteo Moretto,
David Amoyal, Florian Plettenberg**.

## How it works
1. `romano_bot.py` queries a news API for those journalists (one combined OR
   query per poll to stay within the free-tier request budget).
2. Keeps only items whose title/description mention a transfer keyword
   (`here we go`, `confirmed`, `official`, `medical`, `signs`, ...) — a cheap
   prefilter before spending a Claude call.
3. Passes each candidate to **Claude** (`claude-opus-4-8`), which confirms it's
   a real completed transfer and returns a structured briefing: player,
   position, age, clubs, fee, style of play, and how he fits the new team.
4. Sends the briefing to your Telegram chat and records the article ID in
   `state.json` so nothing is processed twice.
5. A GitHub Actions cron runs it every 15 minutes (free, always-on).

Each message looks like:

```
⚽️ Player Name
📍 Right winger · 21
🔄 Selling Club → Buying Club
💰 Fee: €45m
🎮 Style: ...
🧩 Fit: ...
🗞 Source: Fabrizio Romano
```

## One-time setup

### 1. Get the pieces
- **Bot token** — from @BotFather (already have one).
- **Chat id** — send your bot a message, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[].message.chat.id`.
- **News API key** — free tier at https://newsdata.io (200/day) or
  https://gnews.io (100/day). For GNews set `NEWS_PROVIDER=gnews`.
- **Anthropic API key** — from https://console.anthropic.com (for the briefing
  step). Cost is a fraction of a cent per confirmed transfer.

### 2. Test locally
```bash
cp .env.example .env      # fill in the three values
set -a; source .env; set +a
python3 romano_bot.py
```

### 3. Deploy to GitHub Actions
Push this folder to a GitHub repo, then in **Settings → Secrets and variables →
Actions** add repository **secrets**:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `NEWS_API_KEY`
- `ANTHROPIC_API_KEY`

(Optional) add a **variable** `NEWS_PROVIDER` = `gnews` to switch providers.

The workflow (`.github/workflows/poll.yml`) then runs every 15 min and commits
`state.json` updates back to the repo. Trigger a first run manually from the
**Actions** tab → *Romano transfer bot* → *Run workflow*.

## Tuning
- Edit `KEYWORDS` in `romano_bot.py` to widen/narrow what counts as a transfer.
- Change the `cron:` line in the workflow for a different interval.
- Set `NEWS_QUERY` to change who's followed (newsdata.io free tier: max 100
  chars, so use surnames).
