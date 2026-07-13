#!/usr/bin/env python3
"""Poll a news API for transfer news from a set of football journalists, use
Claude to turn each confirmed deal into a structured scouting briefing (player,
clubs, fee, style of play, fit), and push it to a Telegram chat. Designed to run
on a cron (GitHub Actions or launchd). State (already-processed article IDs) is
kept in state.json so the same story is never sent twice.

Required environment variables:
  TELEGRAM_BOT_TOKEN   Bot HTTP API token from @BotFather
  TELEGRAM_CHAT_ID     Your chat id (numeric)
  NEWS_API_KEY         API key for the news provider
  ANTHROPIC_API_KEY    Claude API key (for the briefing step)
Optional:
  NEWS_PROVIDER        "newsdata" (default) or "gnews"
  NEWS_QUERY           Search phrase (default: the six journalists below)
  CLAUDE_MODEL         Model id (default "claude-opus-4-8")
  STATE_FILE           Path to state file (default state.json next to script)
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import anthropic
from pydantic import BaseModel

# Only forward items whose title/description mentions one of these. Keeps the
# feed to confirmed transfers + "here we go" moments instead of every mention.
KEYWORDS = [
    "here we go",
    "confirmed",
    "official",
    "done deal",
    "medical",
    "signs",
    "signed",
    "completes",
    "completed",
    "agreement reached",
    "deal done",
    "joins",
]

# Journalists we follow (for reference / display). The actual API search uses
# NEWS_QUERY below. NOTE: newsdata.io's free tier caps the q string at 100
# chars, so Romano is fully qualified and the rest use their (distinctive)
# surnames to stay under the limit while still matching.
JOURNALISTS = [
    "Fabrizio Romano",
    "David Ornstein",
    "Gianluca Di Marzio",
    "Matteo Moretto",
    "David Amoyal",
    "Florian Plettenberg",
]
NEWS_QUERY = os.environ.get(
    "NEWS_QUERY",
    '"Fabrizio Romano" OR Ornstein OR "Di Marzio" OR Moretto OR Amoyal OR Plettenberg',
)
PROVIDER = os.environ.get("NEWS_PROVIDER", "newsdata").lower()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
MAX_STATE = 500  # cap remembered IDs so state.json doesn't grow forever


class TransferBrief(BaseModel):
    """Structured scouting briefing Claude produces for one article."""

    is_transfer: bool  # true only for a confirmed/official/"here we go" move
    player: str        # the player involved
    position: str      # playing position, e.g. "Right winger" (or "—")
    age: str           # age in years, e.g. "21" (or "—" if unknown)
    from_club: str     # selling club (or "—" if unknown)
    to_club: str       # buying club (or "—" if unknown)
    fee: str           # reported fee, "Free transfer", "Loan", or "Undisclosed"
    style: str         # one sentence on the player's style of play
    fit: str           # one sentence on how he should be used at the new club
    source: str        # journalist/outlet credited with the report (or "—")


BRIEF_SYSTEM = (
    "You are a football transfer analyst. You receive a news headline and "
    "summary about a possible transfer. Decide whether it reports a CONFIRMED "
    "or official/'here we go' completed transfer (not a rumour, contract "
    "renewal, injury, or 'interested in' story) and extract a briefing.\n"
    "- Set is_transfer=false for rumours, links, loans being discussed, "
    "contract extensions, or anything not yet done.\n"
    "- fee: use the reported figure if stated (e.g. '€45m'); otherwise "
    "'Free transfer', 'Loan', or 'Undisclosed'. Never invent a number.\n"
    "- position: the player's playing position (e.g. 'Right winger', "
    "'Centre-back'), from the article or your knowledge; '—' if unknown.\n"
    "- age: the player's age in years as a number; use the age stated in the "
    "article, else your best-known age for the player, else '—'.\n"
    "- style: one concise sentence on the player's playing style.\n"
    "- fit: one concise sentence on how he should be used / why he fits the "
    "new club. Base style and fit on your football knowledge of the player.\n"
    "- source: the journalist or outlet credited with breaking/reporting this "
    "transfer (e.g. 'Fabrizio Romano', 'David Ornstein', 'Sky Sport'), taken "
    "from the article; '—' if not clear.\n"
    "Be factual and concise."
)


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "romano-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_articles():
    """Return a list of {id, title, desc, url, source} from the news provider."""
    key = os.environ["NEWS_API_KEY"]
    if PROVIDER == "gnews":
        q = urllib.parse.quote(NEWS_QUERY)
        url = (
            f"https://gnews.io/api/v4/search?q={q}&lang=en&max=25"
            f"&sortby=publishedAt&apikey={key}"
        )
        data = _get_json(url)
        out = []
        for a in data.get("articles", []):
            out.append({
                "id": a.get("url"),
                "title": a.get("title") or "",
                "desc": a.get("description") or "",
                "url": a.get("url") or "",
                "source": (a.get("source") or {}).get("name", ""),
            })
        return out

    # default: newsdata.io
    q = urllib.parse.quote(NEWS_QUERY)
    url = (
        f"https://newsdata.io/api/1/news?apikey={key}&q={q}"
        f"&language=en&category=sports"
    )
    data = _get_json(url)
    if data.get("status") != "success":
        raise RuntimeError(f"newsdata error: {data}")
    out = []
    for a in data.get("results", []):
        out.append({
            "id": a.get("article_id") or a.get("link"),
            "title": a.get("title") or "",
            "desc": a.get("description") or "",
            "url": a.get("link") or "",
            "source": a.get("source_id", ""),
        })
    return out


def is_transfer(article):
    """Cheap keyword prefilter so we only spend Claude calls on likely deals."""
    text = f"{article['title']} {article['desc']}".lower()
    return any(k in text for k in KEYWORDS)


def brief_article(client, article):
    """Ask Claude for a structured briefing. Returns a TransferBrief or None."""
    prompt = (
        f"Headline: {article['title']}\n"
        f"Summary: {article['desc']}\n"
        f"Source: {article['source']}"
    )
    resp = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=BRIEF_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=TransferBrief,
    )
    return resp.parsed_output


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"sent": []}


def save_state(state):
    state["sent"] = state["sent"][-MAX_STATE:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_telegram(article, brief):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    move = f"{_esc(brief.from_club)} → {_esc(brief.to_club)}"
    # "Right winger · 21" — drop whichever part is unknown, skip the line if both are
    bits = [b for b in (brief.position, brief.age) if b and b.strip() not in ("", "—")]
    text = f"⚽️ <b>{_esc(brief.player)}</b>\n"
    if bits:
        text += f"📍 {_esc(' · '.join(bits))}\n"
    text += (
        f"🔄 {move}\n"
        f"💰 <b>Fee:</b> {_esc(brief.fee)}\n"
        f"🎮 <b>Style:</b> {_esc(brief.style)}\n"
        f"🧩 <b>Fit:</b> {_esc(brief.fit)}"
    )
    if brief.source and brief.source.strip() not in ("", "—"):
        text += f"\n🗞 <b>Source:</b> {_esc(brief.source)}"
    if article["source"]:
        text += f"\n\n<i>{_esc(article['source'])}</i>"
    if article["url"]:
        text += f' · <a href="{article["url"]}">Read more</a>'
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    state = load_state()
    seen = set(state["sent"])
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the env
    try:
        articles = fetch_articles()
    except Exception as e:  # noqa: BLE001
        print(f"fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    sent_count = 0
    # oldest first so messages arrive in chronological order
    for article in reversed(articles):
        if not article["id"] or article["id"] in seen:
            continue
        if not is_transfer(article):
            continue  # keyword prefilter — don't waste a Claude call
        try:
            brief = brief_article(client, article)
        except Exception as e:  # noqa: BLE001 — leave unprocessed, retry next run
            print(f"claude error on '{article['title']}': {e}", file=sys.stderr)
            continue
        # Mark processed regardless of verdict so we don't re-evaluate it.
        seen.add(article["id"])
        state["sent"].append(article["id"])
        if not brief.is_transfer:
            print(f"skipped (not a confirmed transfer): {article['title']}")
            continue
        result = send_telegram(article, brief)
        if result.get("ok"):
            sent_count += 1
            print(f"sent: {brief.player} — {brief.from_club} -> {brief.to_club}")
        else:
            print(f"telegram error: {result}", file=sys.stderr)

    save_state(state)
    print(f"done. {sent_count} briefing(s) sent, {len(articles)} scanned.")


if __name__ == "__main__":
    main()
