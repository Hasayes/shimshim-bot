#!/usr/bin/env python3
"""Poll a news API for transfer news from a set of football journalists, use
Claude to turn each item into a structured scouting briefing (player, clubs,
fee, style of play, fit), and push it to a Telegram chat. Two tracks:
deal-stage news (here we go / medical / completed) for any club, one message
per stage; and interest-stage news (rumours, bids, talks) for the WATCHED_CLUBS
only, one message per player+club pair. Designed to run on a cron (GitHub
Actions or launchd). State (processed article IDs, sent deal stages and
interest pairs) is kept in state.json so the same story is never sent twice.

Required environment variables:
  TELEGRAM_BOT_TOKEN   Bot HTTP API token from @BotFather
  TELEGRAM_CHAT_ID     Your chat id (numeric)
  NEWS_API_KEY         API key for the news provider
  ANTHROPIC_API_KEY    Claude API key (for the briefing step)
Optional:
  NEWS_PROVIDER        "newsdata" (default) or "gnews"
  NEWS_QUERY           Search phrase (default: the six journalists below)
  NEWSDATA_PAGES       newsdata pages per poll, 1 credit each (default 2)
  CLAUDE_MODEL         Model id (default "claude-opus-4-8")
  STATE_FILE           Path to state file (default state.json next to script)
"""
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

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

# Clubs whose transfer INTEREST (rumour-stage) is also notified. Deal-stage
# news is sent for every club; interest-stage only for these.
WATCHED_CLUBS = [
    "Real Madrid",
    "FC Barcelona",
    "Atletico Madrid",
    "Arsenal",
    "Chelsea",
    "Liverpool",
    "Manchester City",
    "Manchester United",
    "Tottenham Hotspur",
    "Bayern Munich",
    "Borussia Dortmund",
    "Paris Saint-Germain",
    "Juventus",
    "Inter",
    "AC Milan",
    "Napoli",
]

# Watched-club aliases: canonical dedup key + regex matched against _norm()ed
# text (lowercase, accents stripped), so 'Barça'/'FC Barcelona'/'Barcelona'
# all map to one key. Order matters: 'inter milan' must hit inter, not milan.
# Word boundaries matter too: \binter\b must not hit "interested" ('Milan' as
# a first name is an unavoidable but rare false positive — Claude judges it).
CLUB_CANON = [
    ("real madrid", r"real madrid"),
    ("barcelona", r"barcelona|\bbarca\b"),
    ("atletico madrid", r"atletico"),
    ("arsenal", r"arsenal"),
    ("chelsea", r"chelsea"),
    ("liverpool", r"liverpool"),
    ("manchester city", r"man(chester)? city"),
    ("manchester united", r"man(chester)? u(ni)?te?d"),
    ("tottenham", r"tottenham|\bspurs\b"),
    ("bayern munich", r"bayern"),
    ("borussia dortmund", r"dortmund"),
    ("psg", r"paris saint[- ]germain|\bpsg\b"),
    ("juventus", r"juventus|\bjuve\b"),
    ("inter", r"\binter\b"),
    ("milan", r"\bmilan\b"),
    ("napoli", r"napoli"),
]
CLUB_RE = re.compile("|".join(pat for _, pat in CLUB_CANON))

# Interest-stage wording that lets an article through to Claude when a
# watched club is mentioned (deal-stage KEYWORDS above still apply to all).
INTEREST_KEYWORDS = [
    "interest",     # also interested
    "keen",
    "target",
    "eyeing",
    "monitor",      # also monitoring
    "talks",
    "bid",
    "offer",
    "enquir",       # enquiry/enquiring
    "approach",
    "linked",
    "want",         # also wants/wanted
    "pursu",        # pursuing/pursuit
    "race",
    "battle",
    "shortlist",
    "considering",
    "scouting",
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
# Pages fetched per poll from newsdata (each page = 10 articles = 1 API
# credit). 2 keeps a full 96-runs/day schedule under the 200-credit free tier.
NEWSDATA_PAGES = int(os.environ.get("NEWSDATA_PAGES", "2"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
MAX_STATE = 500  # cap remembered IDs so state.json doesn't grow forever


class TransferBrief(BaseModel):
    """Structured scouting briefing Claude produces for one article."""

    kind: Literal["deal", "interest", "none"]  # deal = done/effectively done;
                       # interest = watched club pursuing a player; none = skip
    stage: str         # "Here we go", "Medical" or "Completed" ("—" if not a deal)
    player: str        # the player involved
    position: str      # playing position, e.g. "Right winger" (or "—")
    age: str           # age in years, e.g. "21" (or "—" if unknown)
    from_club: str     # selling/current club (or "—" if unknown)
    to_club: str       # buying club; for interest, the watched club(s) pursuing
    fee: str           # reported fee, "Free transfer", "Loan", or "Undisclosed"
    style: str         # one sentence on the player's style of play
    fit: str           # one sentence on how he should be used at the new club
    source: str        # journalist/outlet credited with the report (or "—")


BRIEF_SYSTEM = (
    "You are a football transfer analyst. You receive a news headline and "
    "summary about a possible transfer. Classify it and extract a briefing.\n"
    "- kind='deal' when it reports a transfer that is done or effectively "
    "done: a completed or officially announced signing; a 'here we go' call; "
    "a total/full agreement reached between all parties; a medical that is "
    "booked, underway or passed. A 'here we go' or medical counts even "
    "though the paperwork is not finished yet. Deals to ANY club qualify.\n"
    "- kind='interest' when it credibly reports that one of these watched "
    "clubs is interested in, targeting, bidding for or in talks to sign a "
    f"player, but the deal is not yet agreed: {', '.join(WATCHED_CLUBS)}. "
    "Interest from any other club does NOT count.\n"
    "- kind='none' for everything else: contract renewals/extensions, "
    "injuries, interest from non-watched clubs, players only being offered "
    "or made available, or general transfer-window chatter.\n"
    "- stage: for kind='deal' the furthest stage the article supports — "
    "'Here we go', 'Medical', or 'Completed' (use 'Completed' for "
    "official/announced/done deals); '—' otherwise.\n"
    "- from_club: the player's selling/current club; '—' if unknown. "
    "to_club: the buying club; for kind='interest', the watched club(s) "
    "pursuing him, comma-separated if several.\n"
    "- fee: use the reported figure, bid or asking price if stated (e.g. "
    "'€45m'); otherwise 'Free transfer', 'Loan', or 'Undisclosed'. Never "
    "invent a number.\n"
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

    # default: newsdata.io — free tier returns 10 articles per page, so follow
    # nextPage or a busy news window pushes stories past what one poll can see.
    q = urllib.parse.quote(NEWS_QUERY)
    base = (
        f"https://newsdata.io/api/1/news?apikey={key}&q={q}"
        f"&language=en&category=sports"
    )
    out = []
    page = None
    for _ in range(NEWSDATA_PAGES):
        data = _get_json(base + (f"&page={page}" if page else ""))
        if data.get("status") != "success":
            raise RuntimeError(f"newsdata error: {data}")
        for a in data.get("results", []):
            out.append({
                "id": a.get("article_id") or a.get("link"),
                "title": a.get("title") or "",
                "desc": a.get("description") or "",
                "url": a.get("link") or "",
                "source": a.get("source_id", ""),
            })
        page = data.get("nextPage")
        if not page:
            break
    return out


def _norm(s):
    """Lowercase, strip accents and extra spaces so outlet spellings match."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().replace(".", " ").split())


def _norm_club(club):
    club = _norm(club)
    for canon, pat in CLUB_CANON:
        if re.search(pat, club):
            return canon
    for suffix in (" fc", " cf", " afc"):
        club = club.removesuffix(suffix)
    return club


def _surname(brief):
    """Normalized surname, or None when the player is unknown — a '—'
    placeholder must not glue unrelated deals together. Surname rather than
    full name because outlets vary first-name forms ('Kyran Thompson' vs
    'K. Thompson')."""
    player = _norm(brief.player)
    if not player or player == "—":
        return None
    return player.split()[-1]


def deal_key(brief):
    """One key per move so several outlets covering it produce one message
    per stage (see STAGE_RANK)."""
    surname = _surname(brief)
    return f"{surname} -> {_norm_club(brief.to_club)}" if surname else None


def interest_keys(brief):
    """One key per (player, watched club) pair so each club's interest in a
    player is notified once, but a second club joining the race still is."""
    surname = _surname(brief)
    if not surname:
        return []
    clubs = [_norm_club(c) for c in brief.to_club.split(",")]
    return [f"interest: {surname} -> {c}" for c in clubs if c and c != "—"]


# A deal message is sent when its stage outranks what was already sent for
# that deal — so here we go -> medical -> completed gives three messages,
# but a late lower-stage article after a completed one is suppressed.
STAGE_RANK = {"here we go": 1, "medical": 2, "completed": 3}


def stage_rank(brief):
    return STAGE_RANK.get(_norm(brief.stage), 3)


def is_relevant(article):
    """Cheap keyword prefilter so we only spend Claude calls on likely items:
    deal-stage wording for any club, or interest wording near a watched club."""
    text = _norm(f"{article['title']} {article['desc']}")
    if any(k in text for k in KEYWORDS):
        return True
    return bool(CLUB_RE.search(text)) and any(k in text for k in INTEREST_KEYWORDS)


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
    state = {"sent": [], "deals": {}, "interest": []}
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text()))
        except json.JSONDecodeError:
            pass
    if isinstance(state["deals"], list):
        # migrate pre-stage format (one entry per deal, no rank) to key->rank
        state["deals"] = {k: STAGE_RANK["completed"] for k in state["deals"]}
    state.setdefault("interest", [])
    return state


def save_state(state):
    state["sent"] = state["sent"][-MAX_STATE:]
    state["deals"] = dict(list(state["deals"].items())[-MAX_STATE:])
    state["interest"] = state["interest"][-MAX_STATE:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_telegram(article, brief):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    # "Right winger · 21" — drop whichever part is unknown, skip the line if both are
    bits = [b for b in (brief.position, brief.age) if b and b.strip() not in ("", "—")]
    if brief.kind == "interest":
        text = f"👀 <b>{_esc(brief.player)}</b>\n"
        if bits:
            text += f"📍 {_esc(' · '.join(bits))}\n"
        if brief.from_club and brief.from_club.strip() not in ("", "—"):
            text += f"🏟 <b>Club:</b> {_esc(brief.from_club)}\n"
        text += f"🎯 <b>Interested:</b> {_esc(brief.to_club)}\n"
    else:
        text = f"⚽️ <b>{_esc(brief.player)}</b>\n"
        if bits:
            text += f"📍 {_esc(' · '.join(bits))}\n"
        text += f"🔄 {_esc(brief.from_club)} → {_esc(brief.to_club)}\n"
        if brief.stage and brief.stage.strip() not in ("", "—"):
            text += f"🚦 <b>Stage:</b> {_esc(brief.stage)}\n"
    text += (
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
    deals = state["deals"]  # deal key -> highest stage rank already sent
    interest_sent = set(state["interest"])
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
        if not is_relevant(article):
            continue  # keyword prefilter — don't waste a Claude call
        try:
            brief = brief_article(client, article)
        except Exception as e:  # noqa: BLE001 — leave unprocessed, retry next run
            print(f"claude error on '{article['title']}': {e}", file=sys.stderr)
            continue
        # Mark processed regardless of verdict so we don't re-evaluate it.
        seen.add(article["id"])
        state["sent"].append(article["id"])
        if brief.kind == "none":
            print(f"skipped (no deal or watched-club interest): {article['title']}")
            continue
        if brief.kind == "interest":
            keys = interest_keys(brief)
            if keys and all(k in interest_sent for k in keys):
                print(f"skipped (interest already sent): {article['title']}")
                continue
        else:  # deal
            key = deal_key(brief)
            if key and stage_rank(brief) <= deals.get(key, 0):
                print(f"skipped (stage already sent, {key}): {article['title']}")
                continue
        result = send_telegram(article, brief)
        if result.get("ok"):
            sent_count += 1
            if brief.kind == "interest":
                for k in keys:
                    interest_sent.add(k)
                    state["interest"].append(k)
            elif key:
                deals[key] = stage_rank(brief)
            print(f"sent ({brief.kind}): {brief.player} — "
                  f"{brief.from_club} -> {brief.to_club}")
        else:
            print(f"telegram error: {result}", file=sys.stderr)

    save_state(state)
    print(f"done. {sent_count} briefing(s) sent, {len(articles)} scanned.")


if __name__ == "__main__":
    main()
