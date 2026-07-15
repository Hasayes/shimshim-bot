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
  TELEGRAM_CHANNELS    comma-separated t.me channels to mirror
                       (default "fabrizioromanotg" — Fabrizio Romano)
  CLAUDE_MODEL         Model id (default "claude-sonnet-4-6")
  STATE_FILE           Path to state file (default state.json next to script)
"""
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import anthropic
from bs4 import BeautifulSoup
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
    # Romano's softer confirmation phrasings — "deal happening as expected"
    # (ter Stegen→Ajax) slipped past the stricter list above
    "deal happening",
    "deal agreed",
    "deal in place",
    "verbal agreement",
    "total agreement",
    "set to sign",
    "green light",
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

# Public Telegram channels mirroring journalists' posts, read via the t.me/s/
# web preview (no auth, no API key). Primary fast source; news articles from
# the provider above remain as the safety net.
TELEGRAM_CHANNELS = os.environ.get("TELEGRAM_CHANNELS", "fabrizioromanotg")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Cards go to the app (feed + web push) only; set TELEGRAM_CARDS=1 to also
# send them to the Telegram chat again. Rare operational alerts (e.g. billing
# outage) still use Telegram either way.
TELEGRAM_CARDS = os.environ.get("TELEGRAM_CARDS", "0") == "1"
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
MAX_STATE = 500  # cap remembered IDs so state.json doesn't grow forever

# The PWA (served from docs/ via GitHub Pages) reads this feed; every card
# that goes to Telegram is also appended here, newest first.
FEED_FILE = Path(os.environ.get("FEED_FILE", Path(__file__).with_name("docs") / "feed.json"))
MAX_FEED = 500


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


CLASSIFY_SYSTEM = (
    "You are a football transfer analyst. You receive a news headline and "
    "summary. Classify the story and extract a briefing FROM THE TEXT.\n"
    "- kind='deal' when it reports a transfer that is done or effectively "
    "done: a completed or officially announced signing; a 'here we go' call; "
    "a total/full agreement reached between all parties; a medical that is "
    "booked, underway or passed. Deals to ANY club qualify.\n"
    "- kind='interest' when it credibly reports that one of these watched "
    "clubs is interested in, targeting, bidding for or in talks to sign a "
    f"player, but the deal is not yet agreed: {', '.join(WATCHED_CLUBS)}. "
    "Interest from any other club does NOT count.\n"
    "- kind='none' for everything else: contract renewals/extensions, "
    "injuries, interest from non-watched clubs, or transfer-window chatter.\n"
    "- stage: for kind='deal' — 'Here we go', 'Medical', or 'Completed'; "
    "'—' otherwise.\n"
    "- Facts (clubs, fee, age, position): take them from the article text "
    "first; your background knowledge may be stale — when the article "
    "doesn't state a fact and you aren't confident, use '—' "
    "('Undisclosed' for the fee). Deal facts get verified separately, "
    "so a '—' is always better than a guess.\n"
    "- style/fit: one concise sentence each from your football knowledge.\n"
    "- source: the journalist or outlet credited; '—' if not clear.\n"
    "Be factual and concise."
)


RESEARCH_SYSTEM = (
    "You are a football transfer fact-checker. Given a headline and summary "
    "about a possible transfer, use web search to verify:\n"
    "1. Is this a completed/effectively-done deal (here we go / medical / "
    "official), rumour-stage interest, or something else entirely?\n"
    "2. The player's full name, the club he is CURRENTLY leaving in this "
    "deal (not a former club — squads change, your memory is stale), his "
    "age today, and his position.\n"
    "3. The buying or interested club(s).\n"
    "4. The reported fee.\n"
    "5. The journalist/outlet credited with the story.\n"
    "You have a budget of at most 3 web searches — plan them so you don't "
    "run out mid-task. When you finish searching (or hit the limit), you "
    "MUST end with your bullet-point findings based on whatever you found "
    "so far — never end the turn without findings. Explicitly mark any fact "
    "you could not verify as UNVERIFIED. Never guess from memory."
)


BRIEF_SYSTEM = (
    "You are a football transfer analyst. You receive a news headline and "
    "summary about a possible transfer, plus research notes verified via "
    "live web search. Classify the story and extract a briefing.\n"
    "Trust the research notes and the article over your training memory — "
    "squads change. Any fact marked UNVERIFIED in the notes or absent from "
    "them must be '—' (or 'Undisclosed' for the fee) — never a guess.\n"
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
    "- from_club: the club the player is leaving in this deal, per the "
    "research notes; '—' if unknown. "
    "to_club: the buying club; for kind='interest', the watched club(s) "
    "pursuing him, comma-separated if several.\n"
    "- fee: use the reported figure, bid or asking price if stated (e.g. "
    "'€45m'); otherwise 'Free transfer', 'Loan', or 'Undisclosed'. Never "
    "invent a number.\n"
    "- position: the player's playing position (e.g. 'Right winger', "
    "'Centre-back'), from the article or your knowledge; '—' if unknown.\n"
    "- age: the player's CURRENT age in years as a number, from the article "
    "or the research notes; '—' if unverified.\n"
    "- style: one concise sentence on the player's playing style.\n"
    "- fit: one concise sentence on how he should be used / why he fits the "
    "new club. Base style and fit on your football knowledge of the player.\n"
    "- source: the journalist or outlet credited with breaking/reporting this "
    "transfer (e.g. 'Fabrizio Romano', 'David Ornstein', 'Sky Sport'), taken "
    "from the article; '—' if not clear.\n"
    "Be factual and concise."
)


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "shimshim-bot/1.0"})
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


def fetch_telegram_posts(max_pages=None):
    """Return recent posts from the mirror channels as article dicts.

    Reads the public t.me/s/<channel> web preview — server-rendered HTML,
    no auth or API key. Each page shows ~20 posts; we always walk
    TELEGRAM_PAGES pages back (?before=<msg_id>, ~60 posts ≈ 1.5 days of
    Romano), so posts that scrolled past the first page during an outage
    or a long cron gap are still picked up. Per-post dedup via state.json
    keeps re-reads free.
    """
    if max_pages is None:
        max_pages = int(os.environ.get("TELEGRAM_PAGES", "3"))
    out = []
    for channel in [c.strip() for c in TELEGRAM_CHANNELS.split(",") if c.strip()]:
        before = None
        for _ in range(max_pages):
            url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; shimshim-bot/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                soup = BeautifulSoup(resp.read().decode("utf-8", "replace"), "html.parser")
            page, ids = [], []
            for msg in soup.select("div.tgme_widget_message"):
                post = msg.get("data-post")  # "channel/12345"
                if not post:
                    continue
                ids.append(int(post.rsplit("/", 1)[1]))
                text_div = msg.select_one(".tgme_widget_message_text")
                if text_div is None:
                    continue
                text = text_div.get_text(" ", strip=True)
                if not text:
                    continue  # photo/video post without a caption
                page.append({
                    "id": f"tg:{post}",
                    "title": text[:120],
                    "desc": text,
                    "url": f"https://t.me/{post}",
                    "source": f"Telegram @{channel}",
                })
            if not ids:
                break
            out.extend(page)
            before = min(ids)
    # newest first, matching the news provider's ordering
    out.sort(key=lambda p: int(p["id"].rsplit("/", 1)[1]), reverse=True)
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


def _article_prompt(article):
    return (
        f"Headline: {article['title']}\n"
        f"Summary: {article['desc']}\n"
        f"Source: {article['source']}"
    )


def classify_article(client, article):
    """Cheap first pass, no web search: classify + extract from the text.

    Non-deals stop here (~1/10th the cost of a verified briefing). Deals go
    on to verify_deal() before publishing.
    """
    resp = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": _article_prompt(article)}],
        output_format=TransferBrief,
    )
    return resp.parsed_output


def verify_deal(client, article):
    """Fact-check the article with web search, then extract a briefing.

    Two calls on purpose: combining the server-side web search tool with
    parsed structured output in a single request scrambles the parsed
    fields, so research and extraction are separated.
    """
    prompt = _article_prompt(article)
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    research = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=RESEARCH_SYSTEM,
        messages=messages,
        tools=tools,
    )
    if research.stop_reason == "pause_turn":  # server tool loop paused; resume once
        research = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=RESEARCH_SYSTEM,
            messages=messages + [{"role": "assistant", "content": research.content}],
            tools=tools,
        )
    notes = "\n".join(b.text for b in research.content if b.type == "text")

    resp = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=BRIEF_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"{prompt}\n\nResearch notes (verified via web search):\n{notes}",
        }],
        output_format=TransferBrief,
    )
    return resp.parsed_output


def load_state():
    state = {"sent": [], "deals": {}, "interest": [], "titles": []}
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text()))
        except json.JSONDecodeError:
            pass
    if isinstance(state["deals"], list):
        # migrate pre-stage format (one entry per deal, no rank) to key->rank
        state["deals"] = {k: STAGE_RANK["completed"] for k in state["deals"]}
    state.setdefault("interest", [])
    state.setdefault("titles", [])
    return state


def save_state(state):
    state["sent"] = state["sent"][-MAX_STATE:]
    state["titles"] = state["titles"][-300:]
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


def append_feed(article, brief):
    """Prepend this card to the JSON feed the PWA reads."""
    feed = []
    if FEED_FILE.exists():
        try:
            feed = json.loads(FEED_FILE.read_text())
        except json.JSONDecodeError:
            pass
    feed.insert(0, {
        "id": article["id"],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": brief.kind,
        "stage": brief.stage,
        "player": brief.player,
        "position": brief.position,
        "age": brief.age,
        "from_club": brief.from_club,
        "to_club": brief.to_club,
        "fee": brief.fee,
        "style": brief.style,
        "fit": brief.fit,
        "source": brief.source,
        "outlet": article["source"],
        "url": article["url"],
        "title": article["title"],
    })
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    FEED_FILE.write_text(json.dumps(feed[:MAX_FEED], indent=1))


def send_plain_telegram(text):
    """Bare Telegram message for operational alerts — no Claude involved."""
    payload = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": text,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        data=payload,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def send_web_push(article, brief):
    """Push the card to every subscribed browser (the installed PWA).

    Subscriptions live in the PUSH_SUBSCRIPTIONS secret (JSON array) rather
    than the repo — the repo is public and endpoints shouldn't be. No-op
    until the secret and VAPID key are configured.
    """
    subs_raw = os.environ.get("PUSH_SUBSCRIPTIONS", "")
    pem_file = os.environ.get("VAPID_PEM_FILE", "")
    if not subs_raw or not pem_file:
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("pywebpush not installed; skipping web push", file=sys.stderr)
        return
    if brief.kind == "interest":
        title = f"👀 {brief.to_club} interested in {brief.player}"
    else:
        title = f"⚽️ {brief.player} → {brief.to_club} · {brief.stage}"
    payload = json.dumps({
        "title": title,
        "body": " · ".join(x for x in (brief.fee, brief.source) if x.strip() not in ("", "—")),
        "url": "./",  # tapping the notification opens the app, not the article
    })
    for sub in json.loads(subs_raw):
        try:
            webpush(sub, payload, vapid_private_key=pem_file,
                    vapid_claims={"sub": "mailto:yuval0156@gmail.com"})
        except WebPushException as e:
            print(f"web push error: {e}", file=sys.stderr)


def main():
    state = load_state()
    seen = set(state["sent"])
    deals = state["deals"]  # deal key -> highest stage rank already sent
    interest_sent = set(state["interest"])
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the env
    articles = []
    try:
        articles += fetch_telegram_posts()
    except Exception as e:  # noqa: BLE001 — one source down must not kill the other
        print(f"telegram fetch failed: {e}", file=sys.stderr)
    try:
        articles += fetch_articles()
    except Exception as e:  # noqa: BLE001
        print(f"news fetch failed: {e}", file=sys.stderr)
    if not articles:
        print("all sources failed", file=sys.stderr)
        sys.exit(1)

    sent_count = 0
    # same story from several outlets or several runs: brief it once
    briefed_titles = set(state["titles"])
    # oldest first so messages arrive in chronological order
    for article in reversed(articles):
        if not article["id"] or article["id"] in seen:
            continue
        if not is_relevant(article):
            continue  # keyword prefilter — don't waste a Claude call
        title_key = _norm(article["title"])[:80]
        if title_key in briefed_titles:
            # duplicate headline this run — the first copy carries the story
            seen.add(article["id"])
            state["sent"].append(article["id"])
            print(f"skipped (duplicate headline this run): {article['title']}")
            continue
        try:
            brief = classify_article(client, article)
            if brief.kind == "deal":
                key = deal_key(brief)
                if key and stage_rank(brief) <= deals.get(key, 0):
                    # already carded this deal at this stage — don't pay for
                    # web verification just to re-suppress it
                    seen.add(article["id"])
                    state["sent"].append(article["id"])
                    briefed_titles.add(title_key)
                    state["titles"].append(title_key)
                    print(f"skipped (stage already sent, pre-verify, {key}): {article['title']}")
                    continue
                brief = verify_deal(client, article)
        except Exception as e:  # noqa: BLE001 — leave unprocessed, retry next run
            if "credit balance" in str(e).lower():
                # Billing outage: alert the user (at most once per 12h) and stop
                # hammering the API — unprocessed articles retry next run.
                print("Anthropic credits exhausted — aborting run", file=sys.stderr)
                last = state.get("billing_alert_ts", "")
                now = datetime.now(timezone.utc)
                stale = (not last or (now - datetime.fromisoformat(last)).total_seconds() > 12 * 3600)
                if stale:
                    try:
                        send_plain_telegram(
                            "⚠️ ShimShim is paused: the Anthropic API credit balance "
                            "is exhausted, so stories can't be briefed. Top up at "
                            "console.anthropic.com → Plans & Billing. Pending stories "
                            "will be processed automatically once credits return."
                        )
                        state["billing_alert_ts"] = now.isoformat(timespec="seconds")
                    except Exception as te:  # noqa: BLE001
                        print(f"billing alert failed: {te}", file=sys.stderr)
                break
            print(f"claude error on '{article['title']}': {e}", file=sys.stderr)
            continue
        briefed_titles.add(title_key)
        state["titles"].append(title_key)
        if state.pop("billing_alert_ts", None):
            try:  # first successful brief after an outage — all clear
                send_plain_telegram("✅ ShimShim is back: Anthropic credits restored, catching up on pending stories.")
            except Exception as te:  # noqa: BLE001
                print(f"all-clear alert failed: {te}", file=sys.stderr)
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
        # The app (feed + web push) is the delivery channel; the Telegram
        # chat card is opt-in via TELEGRAM_CARDS.
        append_feed(article, brief)
        try:
            send_web_push(article, brief)
        except Exception as e:  # noqa: BLE001 — push failure must not block the feed
            print(f"web push error: {e}", file=sys.stderr)
        if TELEGRAM_CARDS:
            try:
                result = send_telegram(article, brief)
                if not result.get("ok"):
                    print(f"telegram error: {result}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"telegram error: {e}", file=sys.stderr)
        sent_count += 1
        if brief.kind == "interest":
            for k in keys:
                interest_sent.add(k)
                state["interest"].append(k)
        elif key:
            deals[key] = stage_rank(brief)
        print(f"sent ({brief.kind}): {brief.player} — "
              f"{brief.from_club} -> {brief.to_club}")

    save_state(state)
    print(f"done. {sent_count} briefing(s) sent, {len(articles)} scanned.")


if __name__ == "__main__":
    main()
