#!/usr/bin/env python3
"""Poll a news API for transfer news from a set of football journalists, use
Claude to turn each item into a structured scouting briefing (player, clubs,
fee, style of play, fit), and push it to a Telegram chat. Two tracks:
deal-stage news (here we go / completed) for any club, one message per stage;
and interest-stage news (rumours, bids, talks) for the WATCHED_CLUBS only, one
message per player+club pair. Designed to run on a cron (GitHub Actions or
launchd). State (processed article IDs, sent deal stages and interest pairs)
is kept in state.json so the same story is never sent twice.

Required environment variables:
  TELEGRAM_BOT_TOKEN   Bot HTTP API token from @BotFather
  TELEGRAM_CHAT_ID     Your chat id (numeric)
  NEWS_API_KEY         API key for the news provider
  ANTHROPIC_API_KEY    Claude API key (for the briefing step)
Optional:
  NEWS_PROVIDER        "newsdata" (default) or "gnews"
  NEWS_QUERY           Search phrase (default: the six journalists below)
  NEWSDATA_PAGES       newsdata pages per poll, 1 credit each (default 1)
  TELEGRAM_CHANNELS    comma-separated t.me channels to mirror
                       (default "fabrizioromanotg" — Fabrizio Romano)
  CLAUDE_MODEL         Model for web-verify step (default "claude-sonnet-4-6")
  CLASSIFY_MODEL       Model for classify step (default "claude-haiku-4-5")
  VERIFY_NEWS          "0" to skip web-verify on news deals (default "1")
  VERIFY_INTEREST      "0" to skip web-verify on news rumours (default "1")
  SKIP_VERIFY_TELEGRAM "0" to web-verify Telegram posts (default "1" — trust source)
  CROSS_CHECK          "1" for extra Sonnet pass after verify (default "0")
  WEB_SEARCH_MAX_USES  Web searches per verify (default 3)
  ORACLE_SANITY        "0" to skip free club-oracle gate (default "1")
  ARTICLE_TEXT_MAX     Max chars of article text sent to Claude (default 1200)
  MAX_RUMOUR_AGE_DAYS  Drop interest cards older than this from the live feed (default 7)
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
    ("inter", r"\binter\b(?!\s+miami)"),
    ("milan", r"\bmilan\b"),
    ("napoli", r"napoli"),
    # non-watched clubs that appear often — alias-fold for dedup keys only
    ("west ham", r"west ham"),
    ("brighton", r"brighton"),
    ("newcastle", r"newcastle"),
    ("aston villa", r"aston villa"),
    ("leeds", r"\bleeds\b"),
    ("wolves", r"wolverhampton|\bwolves\b"),
    ("al hilal", r"al.?hilal"),
    ("al nassr", r"al.?nassr"),
    ("marseille", r"marseille"),
    ("sporting", r"sporting (cp|lisbon)|sporting clube"),
]
# Interest filter / prefilter: ONLY the 16 watched clubs. Extra CLUB_CANON
# entries above are for _norm_club / deal dedup (West Ham, Villa, …), not
# for "is this a watched-club rumour?"
WATCHED_CANON = {
    "real madrid", "barcelona", "atletico madrid", "arsenal", "chelsea",
    "liverpool", "manchester city", "manchester united", "tottenham",
    "bayern munich", "borussia dortmund", "psg", "juventus", "inter",
    "milan", "napoli",
}
WATCHED_CLUB_RE = re.compile(
    "|".join(pat for canon, pat in CLUB_CANON if canon in WATCHED_CANON)
)

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

# newsdata.io free tier caps q at 100 chars — Romano fully qualified, rest
# surnames. Override with NEWS_QUERY if needed.
NEWS_QUERY = os.environ.get(
    "NEWS_QUERY",
    '"Fabrizio Romano" OR Ornstein OR "Di Marzio" OR Moretto OR Amoyal OR Plettenberg',
)
PROVIDER = os.environ.get("NEWS_PROVIDER", "newsdata").lower()
# Pages fetched per poll from newsdata (each page = 10 articles = 1 API
# credit). 2 keeps a full 96-runs/day schedule under the 200-credit free tier.
NEWSDATA_PAGES = int(os.environ.get("NEWSDATA_PAGES", "1"))
# Articles older than this are dropped: news feeds sometimes resurface
# years-old stories (a 2022 Pulisic swap rumour arrived as "news").
MAX_ARTICLE_AGE_DAYS = int(os.environ.get("MAX_ARTICLE_AGE_DAYS", "3"))
# Interest cards older than this are archived and removed from the live feed.
MAX_RUMOUR_AGE_DAYS = int(os.environ.get("MAX_RUMOUR_AGE_DAYS", "7"))

# Transfer-window gate: a rumour (interest card) is only published when its
# report date falls inside an open window. Between windows, speculation is
# dropped as noise (see active_window / in_open_window). Deals are never
# gated — a confirmed move is a fact whenever it's announced. Dates are the
# England/PL reference ("MM-DD"); they shift a few days each season, so each
# boundary is overridable per window via repo Variables. WINDOW_GATE=0 turns
# the gate off (every date counts as in-window).
WINDOW_GATE = os.environ.get("WINDOW_GATE", "1") == "1"
SUMMER_WINDOW_OPEN = os.environ.get("SUMMER_WINDOW_OPEN", "06-10")
SUMMER_WINDOW_CLOSE = os.environ.get("SUMMER_WINDOW_CLOSE", "09-01")
WINTER_WINDOW_OPEN = os.environ.get("WINTER_WINDOW_OPEN", "01-01")
WINTER_WINDOW_CLOSE = os.environ.get("WINTER_WINDOW_CLOSE", "02-03")

# Public Telegram channels mirroring journalists' posts, read via the t.me/s/
# web preview (no auth, no API key). Primary fast source; news articles from
# the provider above remain as the safety net.
TELEGRAM_CHANNELS = os.environ.get("TELEGRAM_CHANNELS", "fabrizioromanotg")
CLASSIFY_MODEL = os.environ.get("CLASSIFY_MODEL", "claude-haiku-4-5")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
# Accuracy defaults: Haiku kind-gate → lean classify → web-verify news cards
# (Sonnet + search) → free oracle club check → publish. Telegram from Fabrizio
# stays trusted (SKIP_VERIFY_TELEGRAM). Cross-check remains opt-in.
VERIFY_NEWS = os.environ.get("VERIFY_NEWS", "1") == "1"
VERIFY_INTEREST = os.environ.get("VERIFY_INTEREST", "1") == "1"
SKIP_VERIFY_TELEGRAM = os.environ.get("SKIP_VERIFY_TELEGRAM", "1") == "1"
CROSS_CHECK = os.environ.get("CROSS_CHECK", "0") == "1"
WEB_SEARCH_MAX_USES = max(1, int(os.environ.get("WEB_SEARCH_MAX_USES", "3")))
ORACLE_SANITY = os.environ.get("ORACLE_SANITY", "1") == "1"
# Cap article text sent to Claude — news blurbs and Telegram posts can be long,
# and the model only needs the claim, not the full scrollback.
ARTICLE_TEXT_MAX = max(300, int(os.environ.get("ARTICLE_TEXT_MAX", "1200")))

# Cards go to the app (feed + web push) only; set TELEGRAM_CARDS=1 to also
# send them to the Telegram chat again. Rare operational alerts (e.g. billing
# outage) still use Telegram either way.
TELEGRAM_CARDS = os.environ.get("TELEGRAM_CARDS", "0") == "1"

# DRY_RUN=1: fetch + prefilter only — no Claude calls, no sends, no state or
# feed writes. Lets a feature branch run end-to-end with zero side effects.
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
MAX_STATE = 1500  # cap remembered IDs; must outlast deep Telegram lookbacks

# The PWA (served from docs/ via GitHub Pages) reads this feed; every card
# that goes to Telegram is also appended here, newest first.
FEED_FILE = Path(os.environ.get("FEED_FILE", Path(__file__).with_name("docs") / "feed.json"))
CRESTS_FILE = Path(os.environ.get("FEED_FILE", Path(__file__).with_name("docs") / "feed.json")).with_name("crests.json")
PUSH_META_FILE = Path(os.environ.get("FEED_FILE", Path(__file__).with_name("docs") / "feed.json")).with_name("push-meta.json")
MAX_FEED = 500
ARCHIVE_DIR = Path(os.environ.get("FEED_FILE", Path(__file__).with_name("docs") / "feed.json")).parent / "archive"


def window_of(ts):
    """Map a card timestamp to its transfer window, e.g. '2026-summer'.

    Summer: news from March-September belongs to that year's summer window
    (pre-agreements included). Winter: October-February belongs to the
    January window it feeds (Oct-Dec -> next year's winter).
    """
    d = datetime.fromisoformat(ts)
    if 3 <= d.month <= 9:
        return f"{d.year}-summer"
    if d.month >= 10:
        return f"{d.year + 1}-winter"
    return f"{d.year}-winter"


def active_window(when=None):
    """The open transfer window on a given date, or None between windows.

    Rumours are only surfaced while a window is open; the gap between windows
    is speculative noise, not actionable news. `when` may be a datetime, an
    ISO string (a report's own date), or None (= now). Neither window crosses
    New Year, so a plain "MM-DD" range compare is unambiguous.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    elif isinstance(when, str):
        try:
            when = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            when = datetime.now(timezone.utc)
    md = when.strftime("%m-%d")
    if WINTER_WINDOW_OPEN <= md <= WINTER_WINDOW_CLOSE:
        return f"{when.year}-winter"
    if SUMMER_WINDOW_OPEN <= md <= SUMMER_WINDOW_CLOSE:
        return f"{when.year}-summer"
    return None


def in_open_window(when=None):
    """Whether a rumour reported on `when` may publish: True if a window is
    open then, or the gate is disabled (WINDOW_GATE=0)."""
    return not WINDOW_GATE or active_window(when) is not None


def _window_sort_key(w):
    y, season = w.split("-")
    return (int(y), 0 if season == "winter" else 1)


def archive_cards(cards):
    """Append cards to their windows' archive files (idempotent by id)."""
    if not cards:
        return
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    by_window = {}
    for c in cards:
        by_window.setdefault(window_of(c["ts"]), []).append(c)
    for window, group in by_window.items():
        f = ARCHIVE_DIR / f"{window}.json"
        try:
            existing = json.loads(f.read_text()) if f.exists() else []
        except json.JSONDecodeError:
            existing = []
        seen_ids = {c.get("id") for c in existing}
        existing.extend(c for c in group if c.get("id") not in seen_ids)
        existing.sort(key=lambda c: c["ts"], reverse=True)
        f.write_text(json.dumps(existing, indent=1))
    index = sorted((p.stem for p in ARCHIVE_DIR.glob("*.json") if p.stem != "index"),
                   key=_window_sort_key, reverse=True)
    counts = {}
    for w in index:
        try:
            counts[w] = len(json.loads((ARCHIVE_DIR / f"{w}.json").read_text()))
        except json.JSONDecodeError:
            counts[w] = 0
    (ARCHIVE_DIR / "index.json").write_text(
        json.dumps({"windows": [{"id": w, "cards": counts[w]} for w in index]}, indent=1))


def prune_old_rumours(feed):
    """Archive and drop interest cards older than MAX_RUMOUR_AGE_DAYS."""
    cutoff = datetime.now(timezone.utc).timestamp() - MAX_RUMOUR_AGE_DAYS * 86400
    expired, keep = [], []
    for c in feed:
        if c.get("kind") == "interest":
            try:
                if datetime.fromisoformat(c["ts"]).timestamp() <= cutoff:
                    expired.append(c)
                    continue
            except (ValueError, KeyError):
                pass
        keep.append(c)
    if expired:
        archive_cards(expired)
        print(f"pruned {len(expired)} rumour(s) older than {MAX_RUMOUR_AGE_DAYS} days")
    return keep


def rotate_windows():
    """Move cards from closed windows out of the live feed into the archive."""
    try:
        feed = json.loads(FEED_FILE.read_text())
    except Exception:  # noqa: BLE001
        return
    now_window = window_of(datetime.now(timezone.utc).isoformat())
    past = [c for c in feed if _window_sort_key(window_of(c["ts"])) < _window_sort_key(now_window)]
    if not past:
        return
    archive_cards(past)
    keep = [c for c in feed if c not in past]
    FEED_FILE.write_text(json.dumps(keep, indent=1))
    print(f"rotated {len(past)} card(s) into the archive")


class TransferBrief(BaseModel):
    """Structured scouting briefing Claude produces for one article."""

    kind: Literal["deal", "interest", "none"]  # deal = done/effectively done;
                       # interest = watched club pursuing a player; none = skip
    stage: str         # "Here we go" or "Completed" ("—" if not a deal)
    player: str        # the player involved
    position: str      # playing position, e.g. "Right winger" (or "—")
    age: str           # age in years, e.g. "21" (or "—" if unknown)
    from_club: str     # selling/current club (or "—" if unknown)
    to_club: str       # buying club; for interest, the watched club(s) pursuing
    fee: str           # reported fee, "Free transfer", "Loan", or "Undisclosed"
    style: str         # one sentence on the player's style of play
    fit: str           # one sentence on how he should be used at the new club
    source: str        # journalist/outlet credited with the report (or "—")
    summary: str       # 1-2 factual sentences: what the news actually says


class KindGate(BaseModel):
    """Cheap first triage: kind only, so obvious skips skip the full extract."""

    kind: Literal["deal", "interest", "none"]


class ClassifyBrief(BaseModel):
    """Lean extract used on the hot path — no style/fit (filled only if we publish)."""

    kind: Literal["deal", "interest", "none"]
    stage: str
    player: str
    position: str
    age: str
    from_club: str
    to_club: str
    fee: str
    source: str
    summary: str


class ScoutLines(BaseModel):
    style: str
    fit: str


# Kind rules shared by the tiny gate and the full classify prompt. Prefer
# recall over precision on the gate: a false 'none' drops the story.
_KIND_RULES = (
    "- kind='deal' when it reports a transfer that is done or effectively "
    "done: a completed or officially announced signing; a 'here we go' call; "
    "a total/full agreement reached between all parties; a medical that is "
    "booked, underway or passed; or a finished LOAN RETURN. Deals to ANY "
    "club qualify.\n"
    "- kind='interest' ONLY for REPORTED interest: a named journalist, "
    "outlet or club attributes a CONCRETE step to one of the watched "
    "clubs — opened talks, made contact, submitted or preparing a bid, "
    "agreed personal terms, made him a declared target, pushing to sign. "
    "NOT interest (kind='none'): pundit/ex-player suggestions ('should "
    "sign', 'urged to', 'would be perfect', 'dream signing'); passive "
    "unattributed 'linked with' round-ups and listicles; fan content and "
    "polls; the player's own wishes without club action; agents offering a "
    "player around; hypothetical fits; a watched club setting an asking "
    "price for its OWN player. Watched clubs: "
    f"{', '.join(WATCHED_CLUBS)}. "
    "Interest from any other club does NOT count.\n"
    "- kind='none' for renewals/extensions, injuries, unnamed targets, "
    "stale/recycled old-season stories, or anything that is not "
    "deal/interest above.\n"
)

GATE_SYSTEM = (
    "You triage football transfer headlines. Output only kind. When unsure "
    "between none and deal/interest, prefer deal/interest so nothing "
    "publishable is dropped.\n"
    f"{_KIND_RULES}"
    "The player must be NAMED for deal/interest; otherwise kind='none'."
)

SCOUT_SYSTEM = (
    "Write two short scouting lines for a football transfer card. "
    "style: one sentence on how the player plays. "
    "fit: one sentence on how he should be used at to_club. "
    "If you don't know the player, use '—' for both. No preamble."
)


CLASSIFY_SYSTEM = (
    "You are a football transfer analyst. You receive a news headline and "
    "summary. Classify the story and extract a briefing FROM THE TEXT.\n"
    f"{_KIND_RULES}"
    "- For kind='deal' loan returns: from_club = loan club, to_club = "
    "parent club.\n"
    "- ONE briefing = ONE player: the story's SUBJECT (usually named in "
    "the headline). NEVER substitute a more famous passing mention. "
    "Unnamed-target stories are kind='none'. If several players move, "
    "brief the one in the headline.\n"
    "- stage: for kind='deal' — 'Here we go' (agreed / here-we-go call / "
    "medical booked, underway or passed) or 'Completed' (official, "
    "announced, done); '—' otherwise.\n"
    "- from_club is the club the player currently BELONGS to; a club "
    "mentioned as an interested/rejected suitor or loan host is NOT the "
    "seller — when the article doesn't say whose player he is, use '—'.\n"
    "- Facts (clubs, fee, age, position): take them from the article text "
    "first; your background knowledge may be stale — when the article "
    "doesn't state a fact and you aren't confident, use '—' "
    "('Undisclosed' for the fee). A '—' is always better than a guess.\n"
    "- summary: 1-2 tight factual sentences using only what the article "
    "states. If kind='none', set every other field to '—'.\n"
    "- source: the journalist or outlet credited; '—' if not clear.\n"
    "Be factual and concise."
)


RESEARCH_SYSTEM_TEMPLATE = (
    "You are a football transfer fact-checker. Today is {today}. Given a "
    "headline and summary about a possible transfer, use web search to "
    "verify:\n"
    "0. FIRST: who is this story actually ABOUT? Name its subject — the "
    "player whose move/interest the story reports (usually in the "
    "headline). Other players may appear in passing or as comparisons; "
    "they are not the subject. Also: is this story CURRENT? News feeds "
    "resurface old articles — "
    "if the report actually dates from a previous season, or the player "
    "already plays for a different club than the story implies, state "
    "'STALE STORY' prominently in your findings.\n"
    "1. Is this a completed/effectively-done deal (here we go / medical / "
    "official), rumour-stage interest, or something else entirely?\n"
    "2. The player's full name, age today, position, and — critically — "
    "the club he currently BELONGS to (the parent club that owns his "
    "registration; if he is on loan somewhere, the OWNER, not the loan "
    "club). Beware: articles name clubs in many roles — seller, buyer, "
    "rejected suitors, former clubs, loan clubs. Never assume a club "
    "mentioned in the headline is the seller; verify whose player he IS. "
    "Your findings MUST include a line exactly like: "
    "'CURRENT CLUB (owner): <club or UNVERIFIED>'. EXCEPTION — loan "
    "returns: when a loan ends and the player goes BACK to his parent "
    "club, that return is itself the transfer: state 'LOAN RETURN: from "
    "<loan club> to <parent club>'.\n"
    "3. The buying or interested club(s) — and for interest stories, the "
    "QUALITY of the claim: is a concrete step (talks, contact, bid, "
    "declared target) attributed to a named journalist, outlet or club? "
    "Or is it merely a pundit suggestion, unattributed 'linked with' "
    "round-up, fan content or the writer's own idea? State a line: "
    "'INTEREST QUALITY: reported / weak / opinion'.\n"
    "4. The reported fee.\n"
    "5. The journalist/outlet credited with the story.\n"
    "You have a budget of at most {max_uses} web search(es) — plan them so "
    "you don't run out mid-task. When you finish searching (or hit the limit), "
    "you MUST end with your bullet-point findings based on whatever you found "
    "so far — never end the turn without findings. Explicitly mark any fact "
    "you could not verify as UNVERIFIED. Never guess from memory."
)


def _research_system():
    today = datetime.now(timezone.utc).strftime("%B %Y")
    return RESEARCH_SYSTEM_TEMPLATE.format(
        max_uses=WEB_SEARCH_MAX_USES, today=today
    )


BRIEF_SYSTEM = (
    "You are a football transfer analyst. You receive a news headline and "
    "summary about a possible transfer, plus research notes verified via "
    "live web search. Classify the story and extract a briefing.\n"
    "The player to brief is the story's SUBJECT as established by the "
    "research notes — never a passing mention, however famous. For "
    "interest: if the notes' INTEREST QUALITY line says 'weak' or "
    "'opinion' (or is missing), set kind='none' — only 'reported' "
    "interest publishes. "
    "Trust the research notes and the article over your training memory — "
    "squads change. Any fact marked UNVERIFIED in the notes or absent from "
    "them must be '—' (or 'Undisclosed' for the fee) — never a guess. If "
    "the notes say the story is STALE (recycled old news), set "
    "kind='none'.\n"
    "- kind='deal' also covers a finished LOAN RETURN (player goes back "
    "to his parent club): from_club = loan club, to_club = parent club.\n"
    "- kind='deal' when it reports a transfer that is done or effectively "
    "done: a completed or officially announced signing; a 'here we go' call; "
    "a total/full agreement reached between all parties; a medical that is "
    "booked, underway or passed. A 'here we go' or medical counts even "
    "though the paperwork is not finished yet. Deals to ANY club qualify.\n"
    "- kind='interest' ONLY for REPORTED interest: a named journalist, "
    "outlet or club attributes a CONCRETE step to one of the watched "
    "clubs — opened talks, made contact, submitted or preparing a bid, "
    "agreed personal terms, made him a declared target, pushing to sign. "
    "NOT interest (kind='none'): pundit/ex-player suggestions ('should "
    "sign', 'urged to', 'would be perfect', 'dream signing'); passive "
    "unattributed 'linked with' round-ups and listicles ('5 strikers X "
    "could target'); fan content and polls; the player's own wishes ('I'd "
    "love to play there') without club action; agents offering a player "
    "around; hypothetical fits invented by the writer. A story about a "
    "watched club setting an asking price for its OWN player is NOT "
    "interest (the buyer matters, not the seller). The watched clubs, "
    "for deals not yet agreed: "
    f"{', '.join(WATCHED_CLUBS)}. "
    "Interest from any other club does NOT count.\n"
    "- ONE briefing = ONE player: the player the story is actually ABOUT — "
    "its subject, usually the one named in the headline. NEVER substitute "
    "a different player because he is more famous: players mentioned in "
    "passing, as comparison, as context ('after X left', 'alongside Y') "
    "or in a list of alternatives are NOT the subject. The player must be "
    "NAMED; unnamed-target stories ('a third midfielder') are kind='none'. "
    "If the story genuinely covers several players' own moves, brief the "
    "one in the headline.\n"
    "- kind='none' for everything else: contract renewals/extensions, "
    "injuries, interest from non-watched clubs, players only being offered "
    "or made available, or general transfer-window chatter.\n"
    "- stage: for kind='deal' the furthest stage the article supports — "
    "'Here we go' (agreed / here-we-go call / medical booked, underway or "
    "passed) or 'Completed' (official/announced/done); '—' otherwise.\n"
    "- from_club: MUST be the club from the notes' 'CURRENT CLUB (owner)' "
    "line — the club that owns the player. If that line is missing or "
    "UNVERIFIED, use '—'. Other clubs in the article may be rejected "
    "suitors, former clubs or loan hosts — NEVER cast them as from_club. "
    "EXCEPTION: for a LOAN RETURN (notes say so), from_club = the loan "
    "club being left and to_club = the parent club he returns to. "
    "to_club: the buying club; for kind='interest', the watched club(s) "
    "pursuing him, comma-separated if several.\n"
    "- fee: use the reported figure, bid or asking price if stated (e.g. "
    "'€45m'); otherwise 'Free transfer', 'Loan', or 'Undisclosed'. Never "
    "invent a number.\n"
    "- position: the player's playing position (e.g. 'Right winger', "
    "'Centre-back'), from the article or your knowledge; '—' if unknown.\n"
    "- age: the player's CURRENT age in years as a number, from the article "
    "or the research notes; '—' if unverified.\n"
    "- summary: 1-2 tight factual sentences telling the NEWS itself — what "
    "happened, with the concrete details the story gives (fee structure, "
    "contract length, timing, who reported it). No opinions, no fluff.\n"
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


def same_club(a, b):
    """True when two club name strings refer to the same club."""
    if not a or not b:
        return False
    ka, kb = _norm_club(a), _norm_club(b)
    if ka == kb:
        return True
    ta = {w for w in ka.split() if len(w) > 3}
    tb = {w for w in kb.split() if len(w) > 3}
    return bool(ta and tb and (ta <= tb or tb <= ta))


def oracle_fotmob(player):
    """Current club from FotMob suggest, or '' on miss/error."""
    try:
        d = _get_json(
            "https://apigw.fotmob.com/searchapi/suggest?lang=en&term="
            + urllib.parse.quote(player)
        )
        name = _norm(player)
        parts = name.split()
        if not parts:
            return ""
        surname, first = parts[-1], parts[0] if len(parts) > 1 else ""
        for g in d.get("squadMemberSuggest") or []:
            for o in g.get("options") or []:
                text = _norm((o.get("text") or "").split("|")[0])
                if surname not in text or (first and first not in text):
                    continue
                team = (o.get("payload") or {}).get("teamName") or ""
                if team:
                    return re.sub(r"\s+u\d{2}$", "", team, flags=re.I)
    except Exception:  # noqa: BLE001
        pass
    return ""


def oracle_sportsdb(player):
    """Current club from TheSportsDB soccer players, or '' on miss/error."""
    try:
        d = _get_json(
            "https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p="
            + urllib.parse.quote(player)
        )
        name = _norm(player)
        parts = name.split()
        if not parts:
            return ""
        surname, first = parts[-1], parts[0] if len(parts) > 1 else ""
        for p in d.get("player") or []:
            if (p.get("strSport") or "") != "Soccer":
                continue
            pname = _norm(p.get("strPlayer") or "")
            if surname not in pname or (first and first not in pname):
                continue
            return p.get("strTeam") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def oracle_wikipedia(player):
    """Current club parsed from a Wikipedia footballer intro, or ''."""
    try:
        hits = _get_json(
            "https://en.wikipedia.org/w/api.php?action=query&list=search"
            "&format=json&srlimit=3&srsearch="
            + urllib.parse.quote(f"{player} footballer")
        )["query"]["search"]
        name = _norm(player)
        parts = name.split()
        if not parts:
            return ""
        surname, first = parts[-1], parts[0] if len(parts) > 1 else ""
        titles = [
            h["title"] for h in hits
            if surname in _norm(h.get("title", ""))
            and (not first or first in _norm(h.get("title", "")))
        ]
        if not titles:
            return ""
        d = _get_json(
            "https://en.wikipedia.org/w/api.php?action=query&format=json"
            "&prop=extracts&exintro=1&explaintext=1&exlimit=1&titles="
            + urllib.parse.quote(titles[0])
        )
        pages = (d.get("query") or {}).get("pages") or {}
        extract = next(iter(pages.values()), {}).get("extract") or ""
        m = re.search(
            r"plays? (?:as [^.]{3,40}? )?for (?:[A-Za-z1]+ )?club ([A-Z][^.,]{2,40})",
            extract,
        )
        return m.group(1).strip() if m else ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def oracle_current_clubs(player, max_hits=2):
    """Query free club oracles until max_hits non-empty answers arrive."""
    teams = []
    for oracle in (oracle_fotmob, oracle_sportsdb, oracle_wikipedia):
        t = oracle(player)
        if t:
            teams.append(t)
        if len(teams) >= max_hits:
            break
    return teams


def oracle_sanity_check(brief):
    """Free live-squad check — kill or correct cards that contradict reality.

    Catches the failure modes web-search-off published: recycled Completed
    deals for players who already moved, interest in a club the player
    already plays for, and inverted/wrong from_club. Never invents a card;
    only drops or lightly corrects.
    """
    if not ORACLE_SANITY or brief.kind == "none":
        return brief
    player = (brief.player or "").strip()
    if not player or player == "—":
        return brief
    # Surname-only names collide with namesakes for club corrections, but
    # interest in a club the top FotMob surname-hit already plays for is
    # still a safe kill (Dembele -> PSG while Ousmane is already there).
    if len(_norm(player).split()) < 2:
        if brief.kind == "interest":
            dests = [c.strip() for c in brief.to_club.split(",")
                     if c.strip() not in ("", "—")]
            team = oracle_fotmob(player)
            if team and any(same_club(team, d) for d in dests):
                print(f"oracle sanity: drop interest — {player} already at {team}")
                brief.kind = "none"
        return brief
    teams = oracle_current_clubs(player, max_hits=2)
    if not teams:
        return brief
    dests = [c.strip() for c in brief.to_club.split(",") if c.strip() not in ("", "—")]
    origin = brief.from_club
    at_dest = [d for d in dests if any(same_club(t, d) for t in teams)]
    at_origin = any(same_club(t, origin) for t in teams) if known_club(origin) else False

    # Player already at a destination club
    if at_dest:
        if brief.kind == "interest":
            print(f"oracle sanity: drop interest — {player} already at {at_dest[0]}")
            brief.kind = "none"
            return brief
        if brief.kind == "deal" and _norm(brief.stage) != "completed":
            print(f"oracle sanity: upgrade {player} -> {at_dest[0]} to Completed")
            brief.stage = "Completed"
            brief.to_club = at_dest[0]
        return brief

    # Two oracles agree on a club that is neither origin nor destination
    if len(teams) >= 2 and same_club(teams[0], teams[1]):
        agreed = teams[0]
        if not at_origin and not any(same_club(agreed, d) for d in dests):
            if brief.kind == "deal" and _norm(brief.stage) == "completed":
                # Completed deal to X but oracles still show Y elsewhere —
                # likely a recycled/wrong card. Drop rather than publish.
                print(f"oracle sanity: drop completed {player} -> {brief.to_club} "
                      f"(oracles show {agreed})")
                brief.kind = "none"
                return brief
            if known_club(origin) and not same_club(origin, agreed):
                print(f"oracle sanity: correct from_club {origin} -> {agreed} "
                      f"for {player}")
                brief.from_club = agreed
            elif not known_club(origin):
                brief.from_club = agreed
    return brief


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
                "published": a.get("publishedAt") or "",  # for the window gate
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
            pub = a.get("pubDate") or ""
            published = ""
            if pub:
                try:
                    dt = datetime.strptime(
                        pub, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    published = dt.isoformat()  # for the transfer-window gate
                    if (datetime.now(timezone.utc) - dt).days >= MAX_ARTICLE_AGE_DAYS:
                        continue  # resurfaced old story — not news
                except ValueError:
                    pass
            out.append({
                "id": a.get("article_id") or a.get("link"),
                "title": a.get("title") or "",
                "desc": a.get("description") or "",
                "url": a.get("link") or "",
                "source": a.get("source_id", ""),
                "published": published,
            })
        page = data.get("nextPage")
        if not page:
            break
    return out


def fetch_telegram_posts(max_pages=None, seen=None):
    """Return recent posts from the mirror channels as article dicts.

    Reads the public t.me/s/<channel> web preview — server-rendered HTML,
    no auth or API key. Each page shows ~20 posts; we walk up to
    TELEGRAM_PAGES pages back (?before=<msg_id>) so posts that scrolled
    past the first page during an outage or long cron gap are still
    picked up. Stops early once a page's post ids are all already in
    seen (state.json), so steady-state polls usually fetch one page.
    """
    if max_pages is None:
        max_pages = int(os.environ.get("TELEGRAM_PAGES", "3"))
    seen = seen or set()
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
                tstamp = msg.select_one("time[datetime]")  # for the window gate
                page.append({
                    "id": f"tg:{post}",
                    "title": text[:120],
                    "desc": text,
                    "url": f"https://t.me/{post}",
                    "source": f"Telegram @{channel}",
                    "published": tstamp.get("datetime", "") if tstamp else "",
                })
            if not ids:
                break
            out.extend(page)
            # Older pages only have older posts — stop once this page is fully known.
            if all(f"tg:{channel}/{i}" in seen for i in ids):
                break
            before = min(ids)
    # newest first, matching the news provider's ordering
    out.sort(key=lambda p: int(p["id"].rsplit("/", 1)[1]), reverse=True)
    return out


def _norm(s):
    """Lowercase, strip accents/hyphens and extra spaces so spellings match."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().replace(".", " ").replace("-", " ").split())


# --- Source trust: corroboration + tier ------------------------------------
# A mostly-objective trust signal to replace the web-verify we turned off.
# We do NOT editorialise the long tail; we only whitelist club/league officials
# and the well-established transfer ITKs, and let CORROBORATION (independent
# sources agreeing) carry the rest — a card named by 3 outlets is trustworthy
# regardless of who they are; a lone unknown handle is where caution is due.
OFFICIAL_MARKERS = ("official", "website", "club statement", "medical complete")
TIER1_SOURCES = (
    "fabrizio romano", "david ornstein", "the athletic", "gianluca di marzio",
    "di marzio", "matteo moretto", "nicolo schira", "florian plettenberg",
    "christian falk", "sky sports", "sky sport", "sky germany", "sky italia",
    "paul joyce", "craig hope", "bbc", "reuters", "l equipe",
)
_SOURCE_SPLIT = re.compile(r"\s*[;,/]\s*|\s+and\s+", re.I)


def split_sources(raw):
    """Free-text source string -> distinct, trimmed reporter/outlet names.

    Splits on ',', ';', '/' and ' and ', drops parenthetical asides
    ('(City interest)'), and dedupes by normalized key so 'Fabrizio Romano'
    and 'fabrizio romano' collapse to one."""
    if not raw or raw.strip() in ("", "—"):
        return []
    seen, out = set(), []
    for part in _SOURCE_SPLIT.split(raw):
        name = re.sub(r"\s*\(.*?\)\s*", " ", part).strip().strip(".")
        key = _norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def tier_of(names):
    """official > tier1 > standard, from a list of source names."""
    joined = " || ".join(_norm(n) for n in names)
    if any(m in joined for m in OFFICIAL_MARKERS):
        return "official"
    if any(t in joined for t in TIER1_SOURCES):
        return "tier1"
    return "standard"


def source_meta(raw):
    """(distinct source names, trust tier) for a free-text source string."""
    names = split_sources(raw)
    return names, tier_of(names)


def merge_sources(existing, new):
    """Order-stable union of two source-name lists, deduped by normalized key.
    This is where cross-poll corroboration accumulates: the same journey
    reported by a second outlet in a later poll grows this list."""
    seen, out = set(), []
    for name in list(existing or []) + list(new or []):
        key = _norm(re.sub(r"\(.*?\)", "", name))
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def known_club(club):
    return bool(club) and club.strip() not in ("", "—")


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
# that deal — so here we go -> completed gives two messages, but a late
# lower-stage article after a completed one is suppressed.
STAGE_RANK = {"here we go": 1, "completed": 2}


def stage_rank(brief):
    return STAGE_RANK.get(_norm(brief.stage), 2)


def is_relevant(article):
    """Cheap keyword prefilter so we only spend Claude calls on likely items:
    deal-stage wording for any club, or interest wording near a watched club."""
    text = _norm(f"{article['title']} {article['desc']}")
    if any(k in text for k in KEYWORDS):
        return True
    return bool(WATCHED_CLUB_RE.search(text)) and any(k in text for k in INTEREST_KEYWORDS)


def _clip_text(text, limit=ARTICLE_TEXT_MAX):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _article_prompt(article):
    """Compact article text for Claude — dedupe Telegram title/body and cap length."""
    title = (article.get("title") or "").strip()
    desc = (article.get("desc") or "").strip()
    source = (article.get("source") or "").strip()
    # Telegram posts set title = text[:120] and desc = full text; send once.
    if desc and title and (desc == title or desc.startswith(title)):
        body = _clip_text(desc)
        prompt = f"Text: {body}"
    else:
        prompt = f"Headline: {_clip_text(title, 160)}\nSummary: {_clip_text(desc)}"
    if source:
        prompt += f"\nSource: {source}"
    return prompt


VALID_STAGES = {"here we go", "completed"}

# Role/position phrases the model sometimes puts in player= when the article
# never names the footballer ("Backup goalkeeper", "a third midfielder").
# One-name stars (Endrick, Rodri) still pass — this only rejects role words.
_ROLE_PLAYER_RE = re.compile(
    r"^(?:(?:the|a|an|his|their|its)\s+)?"
    r"(?:(?:backup|deputy|reserve|second|third|another|unnamed|young|new)\s+)?"
    r"(?:goalkeepers?|keepers?|strikers?|forwards?|wingers?|midfielders?|"
    r"defenders?|centre-?backs?|center-?backs?|full-?backs?|right-?backs?|"
    r"left-?backs?|attackers?|players?)$",
    re.I,
)


def brief_problems(brief):
    """Structural lint a card must pass before publishing.

    A card with missing core facts ("—" player, deal without clubs) looks
    broken in the app and can't dedup properly — better no card than an
    empty one; the story returns via other headlines with fuller facts.
    """
    problems = []
    player = brief.player.strip()
    if player in ("", "—") or len(player) < 2:
        problems.append("no player")
    elif len(player) > 60:
        problems.append("implausible player name")
    elif _ROLE_PLAYER_RE.match(_norm(player)):
        problems.append("unnamed/role player")
    if brief.to_club.strip() in ("", "—"):
        problems.append("no destination/suitor club")
    if brief.kind == "deal":
        if brief.from_club.strip() in ("", "—"):
            problems.append("deal without origin club")
        if _norm(brief.stage) not in VALID_STAGES:
            problems.append(f"invalid stage {brief.stage!r}")
    if brief.kind == "interest" and brief.from_club.strip() in ("", "—"):
        # A real rumour always knows where the player currently plays; a blank
        # origin is the tell for a misparse — a lone first name ("Enzo") or a
        # fabricated/stale link (a player long gone from the club named).
        # Cheap catch for the class lean mode can't web-verify after the fact.
        problems.append("rumour without origin club")
    return problems


def classify_kind(client, article):
    """Tiny triage call — most keyword false-positives die here as kind=none."""
    resp = client.messages.parse(
        model=CLASSIFY_MODEL,
        max_tokens=64,
        system=GATE_SYSTEM,
        messages=[{"role": "user", "content": _article_prompt(article)}],
        output_format=KindGate,
    )
    return resp.parsed_output


def classify_article(client, article):
    """Lean extract from the text (no style/fit — those are filled on publish).

    Non-deals stop here (~1/10th the cost of a verified briefing). Deals go
    on to verify_deal() before publishing when needs_web_verify() says so.
    """
    resp = client.messages.parse(
        model=CLASSIFY_MODEL,
        max_tokens=512,
        system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": _article_prompt(article)}],
        output_format=ClassifyBrief,
    )
    data = resp.parsed_output.model_dump()
    return TransferBrief(**data, style="—", fit="—")


def fill_scout_lines(client, article, brief):
    """Generate style/fit only for cards that are about to publish."""
    if (brief.style or "").strip() not in ("", "—") and \
            (brief.fit or "").strip() not in ("", "—"):
        return brief
    resp = client.messages.parse(
        model=CLASSIFY_MODEL,
        max_tokens=256,
        system=SCOUT_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"{_article_prompt(article)}\n\n"
                f"Player: {brief.player}\n"
                f"Position: {brief.position}\n"
                f"From: {brief.from_club}\n"
                f"To: {brief.to_club}\n"
                f"Summary: {brief.summary}"
            ),
        }],
        output_format=ScoutLines,
    )
    lines = resp.parsed_output
    brief.style = lines.style or "—"
    brief.fit = lines.fit or "—"
    return brief


def needs_web_verify(article, brief):
    """Whether to run verify_deal() (web search) before publishing.

    Accuracy default: web-verify every news-sourced deal and rumour.
    Trusted Telegram mirrors (Fabrizio) skip search when
    SKIP_VERIFY_TELEGRAM=1 — their posts are the primary source of truth.
    """
    is_telegram = (
        article["id"].startswith("tg:")
        or "telegram" in _norm(article.get("source", ""))
    )
    if is_telegram and SKIP_VERIFY_TELEGRAM:
        return False
    if brief.kind == "interest":
        return VERIFY_INTEREST
    return VERIFY_NEWS


def verify_deal(client, article):
    """Fact-check the article with web search, then extract a briefing.

    Two calls on purpose: combining the server-side web search tool with
    parsed structured output in a single request scrambles the parsed
    fields, so research and extraction are separated.
    """
    prompt = _article_prompt(article)
    messages = [{"role": "user", "content": prompt}]
    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": WEB_SEARCH_MAX_USES,
    }]
    research = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=_research_system(),
        messages=messages,
        tools=tools,
    )
    if research.stop_reason == "pause_turn":  # server tool loop paused; resume once
        research = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=_research_system(),
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
    draft = resp.parsed_output
    if draft.kind == "none" or not CROSS_CHECK:
        return draft
    # Optional final cross-examination (CROSS_CHECK=1): catches extraction
    # slips but doubles Sonnet spend on verified cards.
    check = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=(
            "You are the final verifier for a transfer news card. Compare "
            "the DRAFT card against the RESEARCH NOTES and output the "
            "corrected card in the same schema. Change ONLY fields the "
            "notes contradict; keep everything else identical. Enforce: "
            "from_club is the notes' CURRENT CLUB owner (loan-return "
            "exception applies); to_club holds exactly the reported "
            "buyer/suitors; stage only as far as the notes support; the "
            "summary contains only facts present in the notes or article. "
            "If the card's core claim is not supported at all, set "
            "kind='none'."
        ),
        messages=[{
            "role": "user",
            "content": f"ARTICLE:\n{prompt}\n\nRESEARCH NOTES:\n{notes}\n\n"
                       f"DRAFT CARD:\n{draft.model_dump_json(indent=1)}",
        }],
        output_format=TransferBrief,
    )
    final = check.parsed_output
    if final.kind != draft.kind or final.from_club != draft.from_club \
            or final.to_club != draft.to_club or final.stage != draft.stage:
        print(f"cross-check corrected: {draft.player} "
              f"[{draft.from_club}->{draft.to_club}/{draft.stage}] => "
              f"[{final.from_club}->{final.to_club}/{final.stage}] kind={final.kind}")
    return final


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


def _title_name_head(title):
    """Normalized page/player name with a parenthetical disambiguator stripped.

    'Moussa Dembélé (French footballer)' -> 'moussa dembele'
    'Rodri (footballer, born 1996)' -> 'rodri'
    """
    return _norm(title).split("(")[0].strip()


def _name_matches_query(display_name, surname, first):
    """True when a candidate display name matches the card's player query.

    Full name (first+surname): both tokens must appear — stops Elijah Upson
    latching onto father Matthew Upson.
    Surname-only: only mononym pages/suggestions ('Endrick', 'Rodri') match.
    'Dembele' must not accept 'Moussa Dembélé' / 'Ousmane Dembélé' — that
    path once put Moussa's Celtic face on an Ousmane/PSG interest card
    because Moussa's wiki intro said 'Developed at Paris Saint-Germain'.
    """
    head = _title_name_head(display_name)
    if not head or surname not in head:
        return False
    if first:
        return first in head
    return head == surname


def _wikipedia_photo(player, clubs_text):
    """Second-chance lookup: Wikipedia disambiguates namesakes properly.

    A candidate page's photo is accepted only when the page's own intro
    mentions one of the card's clubs — that text names the person's club,
    so a match confirms identity (the Alex Scott namesake trap). Uses the
    batch action API (one call for all candidates) because the REST
    summary endpoint throttles anonymous per-page bursts.
    """
    want = {w for w in _norm(clubs_text.replace("|", " ")).split() if len(w) > 3}
    surname = _norm(player).split()[-1] if _norm(player) else ""
    try:
        hits = _get_json(
            "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
            "&srlimit=5&srsearch=" + urllib.parse.quote(f"{player} footballer")
        )["query"]["search"]
        name_parts = _norm(player).split()
        first = name_parts[0] if len(name_parts) > 1 else ""
        titles = [h["title"] for h in hits[:5]
                  if _name_matches_query(h.get("title", ""), surname, first)]
        if not titles:
            return ""
        data = _get_json(
            "https://en.wikipedia.org/w/api.php?action=query&format=json"
            "&prop=extracts%7Cpageimages&exintro=1&explaintext=1&exlimit=5"
            "&piprop=thumbnail&pithumbsize=330&titles="
            + urllib.parse.quote("|".join(titles))
        )
        pages = (data.get("query") or {}).get("pages") or {}
        # preserve search ranking, not the API's arbitrary page order
        by_title = {p.get("title"): p for p in pages.values()}
        for title in titles:
            p = by_title.get(title) or {}
            extract = _norm(p.get("extract") or "")
            if "footballer" not in extract and "football player" not in extract:
                continue
            if not any(w in extract.split() for w in want):
                continue
            thumb = (p.get("thumbnail") or {}).get("source", "")
            if thumb:
                return thumb
    except Exception as e:  # noqa: BLE001
        print(f"wikipedia photo lookup failed for {player}: {e}", file=sys.stderr)
    return ""


def _fotmob_photo(player, clubs_text):
    """Third-chance lookup: FotMob covers academy/youth players the other
    sources miss. Same discipline: the suggestion's team (with U18/U21/U23
    suffixes stripped) must match one of the card's clubs, and the name
    must match first+last — otherwise no photo.
    """
    name_parts = _norm(player).split()
    if not name_parts:
        return ""
    surname, first = name_parts[-1], name_parts[0] if len(name_parts) > 1 else ""
    want_canon = {_norm_club(c) for c in clubs_text.split("|") if c.strip()}
    want_words = {w for w in _norm(clubs_text.replace("|", " ")).split() if len(w) > 3}
    try:
        data = _get_json(
            "https://apigw.fotmob.com/searchapi/suggest?lang=en&term="
            + urllib.parse.quote(player)
        )
        for group in data.get("squadMemberSuggest") or []:
            for opt in group.get("options") or []:
                payload = opt.get("payload") or {}
                if payload.get("isCoach"):
                    continue
                text = (opt.get("text") or "").split("|")[0]
                if not _name_matches_query(text, surname, first):
                    continue
                team = re.sub(r"\s+u\d{2}$", "", _norm(payload.get("teamName") or ""))
                if not team:
                    continue
                if _norm_club(team) not in want_canon and \
                        not any(w in want_words for w in team.split() if len(w) > 3):
                    continue
                pid = payload.get("id")
                if not pid:
                    continue
                url = f"https://images.fotmob.com/image_resources/playerimages/{pid}.png"
                req = urllib.request.Request(url, method="HEAD",
                                             headers={"User-Agent": "shimshim-bot/1.0"})
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        if resp.status == 200:
                            return url
                except Exception:  # noqa: BLE001
                    continue
    except Exception as e:  # noqa: BLE001
        print(f"fotmob photo lookup failed for {player}: {e}", file=sys.stderr)
    return ""


def lookup_player_photo(player, clubs_text, state):
    """Best-effort player photo from TheSportsDB (free tier).

    Accuracy first: a unique search hit is accepted; with several hits we
    require the database's team to match one of the card's clubs, else no
    photo (the crest fallback looks fine, a wrong face does not). Results
    (including misses) are cached in state to avoid repeat lookups.
    """
    name = _norm(player)
    if not name or name == "—":
        return ""
    cache = state.setdefault("photos", {})
    if name in cache:
        return cache[name]
    photo = ""
    try:
        data = _get_json(
            "https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p="
            + urllib.parse.quote(player)
        )
        players = data.get("player") or []
        # STRICT match, no unique-hit shortcut: a lone result can still be
        # the wrong person (an "Alex Scott" search once returned a baseball
        # pitcher). Requirements: sport is Soccer AND the DB team matches
        # one of the card's clubs. No confident match -> no photo.
        want_canon = {_norm_club(c) for c in clubs_text.split("|") if c.strip()}
        want_words = {w for w in _norm(clubs_text.replace("|", " ")).split() if len(w) > 3}
        cand = None
        for p in players:
            if (p.get("strSport") or "") != "Soccer":
                continue
            team = p.get("strTeam") or ""
            if not team:
                continue
            if _norm_club(team) in want_canon or \
                    any(w in want_words for w in _norm(team).split() if len(w) > 3):
                cand = p
                break
        if cand:
            pic = cand.get("strCutout") or cand.get("strThumb") or ""
            photo = pic + "/small" if pic else ""
        if not photo:
            photo = _wikipedia_photo(player, clubs_text)
        if not photo:
            photo = _fotmob_photo(player, clubs_text)
    except Exception as e:  # noqa: BLE001 — photos are decoration, never fatal
        print(f"photo lookup failed for {player}: {e}", file=sys.stderr)
        return ""  # transient failure (e.g. rate limit) — do NOT cache as a miss
    cache[name] = photo
    if len(cache) > 300:
        for k in list(cache)[: len(cache) - 300]:
            del cache[k]
    return photo


def ensure_club_crests(brief, state):
    """Resolve badge URLs for any club on the card and publish docs/crests.json.

    The app has static crests only for the 16 watched clubs; everything else
    (Fulham, Como, ...) showed a plain monogram. TheSportsDB serves badges
    for any club — matched strictly (Soccer + normalized name equality) and
    cached in state so each club costs one lookup ever.
    """
    cache = state.setdefault("crests", {})
    clubs = [brief.from_club] + brief.to_club.split(",")
    changed = False
    for raw in clubs:
        club = raw.strip()
        if club in ("", "—"):
            continue
        key = _norm_club(club)
        if key in cache:
            continue
        url = ""
        try:
            data = _get_json(
                "https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t="
                + urllib.parse.quote(club)
            )
            NOISE = {"fc", "afc", "cf", "ac", "as", "rc", "sc", "cd", "ca",
                     "ogc", "tsg", "krc", "bsc", "rb", "ud", "vfb", "vfl",
                     "club", "cp", "olympique", "de", "city", "united", "town",
                     "hotspur", "albion", "1907"}
            def toks(s):
                return {w for w in _norm(s).replace("&", " ").replace("-", " ").split()
                        if w not in NOISE}
            soccer = [t for t in data.get("teams") or []
                      if (t.get("strSport") or "") == "Soccer"]
            if not soccer:  # "AS Monaco" finds nothing; "Monaco" does
                retry_q = " ".join(sorted(toks(club)))
                if retry_q and _norm(retry_q) != _norm(club):
                    data = _get_json(
                        "https://www.thesportsdb.com/api/v1/json/3/searchteams.php?t="
                        + urllib.parse.quote(retry_q)
                    )
                    soccer = [t for t in data.get("teams") or []
                              if (t.get("strSport") or "") == "Soccer"]
            match = None
            for t in soccer:  # pass 1: exact name or listed alternate name
                names = [t.get("strTeam") or "", t.get("strTeamShort") or ""]
                names += (t.get("strTeamAlternate") or "").split(",")
                if any(_norm_club(n) == key for n in names if n.strip()):
                    match = t
                    break
            if match is None:  # pass 2: core-token match, only if unambiguous
                want = toks(club)
                cands = [t for t in soccer
                         if want and (want <= toks(t.get("strTeam") or "")
                                      or toks(t.get("strTeam") or "") <= want)]
                if len(cands) == 1:
                    match = cands[0]
            if match:
                badge = match.get("strBadge") or ""
                if badge:
                    url = badge + "/small"
            if not url:
                # TheSportsDB free search returns one best match and can hand
                # back a same-named netball/hockey club — FotMob covers the rest
                fdata = _get_json(
                    "https://apigw.fotmob.com/searchapi/suggest?lang=en&term="
                    + urllib.parse.quote(club)
                )
                want = toks(club)
                for group in fdata.get("teamSuggest") or []:
                    for opt in group.get("options") or []:
                        tname = (opt.get("text") or "").split("|")[0]
                        tt = toks(tname)
                        if not want or not (want <= tt or tt <= want):
                            continue
                        if re.search(r"u\d{2}$", _norm(tname)):
                            continue  # senior badge, not the U18/U21 entry
                        tid = (opt.get("payload") or {}).get("id")
                        if tid:
                            url = f"https://images.fotmob.com/image_resources/logo/teamlogo/{tid}.png"
                        break
                    if url:
                        break
        except Exception as e:  # noqa: BLE001 — badges are decoration
            print(f"crest lookup failed for {club}: {e}", file=sys.stderr)
            continue  # transient — don't cache
        cache[key] = url
        changed = True
    if changed:
        published = {k: v for k, v in cache.items() if v}
        CRESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CRESTS_FILE.write_text(json.dumps(published, sort_keys=True))


def _card_sig(kind, stage, player, to_club):
    dests = frozenset(_norm_club(c) for c in to_club.split(",") if c.strip())
    return (_norm(player), kind, _norm(stage), dests)


def append_feed(article, brief, photo="", feed=None):
    """Upsert the card: one card per transfer journey, moving through stages.

    Rumour -> Here we go -> Confirmed is ONE card that upgrades in place
    (suitors collapse to the winning club when a deal stage arrives) and
    jumps to the top of the feed. A brand-new story appends a new card.

    Returns 'noop' | 'upgraded' | 'new'. When feed= is passed, mutates that
    list in place and does not write disk (caller writes once at end).
    """
    write = feed is None
    if feed is None:
        feed = []
        if FEED_FILE.exists():
            try:
                feed = json.loads(FEED_FILE.read_text())
            except json.JSONDecodeError:
                pass
    sig = _card_sig(brief.kind, brief.stage, brief.player, brief.to_club)
    dup = next((c for c in feed
                if _card_sig(c["kind"], c.get("stage", ""), c["player"], c["to_club"]) == sig),
               None)
    if dup is not None:
        # Same journey at the same stage already exists. Not a re-stage — but a
        # DIFFERENT outlet reporting the same thing is corroboration, so merge
        # its source in (this is the signal the sig-dedup used to throw away).
        before = dup.get("sources") or split_sources(dup.get("source", ""))
        merged = merge_sources(before, split_sources(brief.source))
        if len(merged) > len(before):
            dup["sources"] = merged
            dup["srcTier"] = tier_of(merged)
            if write:
                FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
                FEED_FILE.write_text(json.dumps(feed[:MAX_FEED], indent=1))
            print(f"corroborated: {brief.player} now {len(merged)} sources")
        else:
            print(f"feed append skipped (identical card exists): {brief.player}")
        return "noop"

    player_key = _norm(brief.player)
    existing = None
    if brief.kind == "deal":
        dest = _norm_club(brief.to_club)
        for c in feed:
            if _norm(c["player"]) != player_key:
                continue
            if c["kind"] == "interest":
                existing = c  # the rumour that became a deal
                break
            if c["kind"] == "deal" and _norm_club(c["to_club"]) == dest \
                    and STAGE_RANK.get(_norm(c.get("stage", "")), 0) < stage_rank(brief):
                existing = c  # the same journey at a lower stage
                break
    elif brief.kind == "interest":
        for c in feed:
            if _norm(c["player"]) == player_key and c["kind"] == "interest":
                existing = c  # merge the new suitor(s) into the player's card
                break

    if existing is not None:
        if brief.kind == "interest":
            seen_clubs, suitors = set(), []
            for raw in (existing["to_club"] + "," + brief.to_club).split(","):
                s = raw.strip()
                if s and s != "—" and _norm_club(s) not in seen_clubs:
                    seen_clubs.add(_norm_club(s))
                    suitors.append(s)
            existing["to_club"] = ", ".join(suitors)
        else:
            existing["kind"] = "deal"
            existing["stage"] = brief.stage
            existing["to_club"] = brief.to_club
        # snapshot prior sources BEFORE the field loop overwrites "source"
        prior_sources = existing.get("sources") or split_sources(existing.get("source", ""))
        for field, val in (("from_club", brief.from_club), ("fee", brief.fee),
                           ("position", brief.position), ("age", brief.age),
                           ("style", brief.style), ("fit", brief.fit),
                           ("source", brief.source), ("summary", brief.summary)):
            if (val or "").strip() not in ("", "—"):
                existing[field] = val
        # accumulate distinct sources across polls -> corroboration signal
        existing["sources"] = merge_sources(prior_sources, split_sources(brief.source))
        existing["srcTier"] = tier_of(existing["sources"])
        if photo and not existing.get("photo"):
            existing["photo"] = photo
        existing["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        existing["outlet"] = article["source"]
        existing["url"] = article["url"]
        existing["title"] = article["title"]
        feed.remove(existing)
        feed.insert(0, existing)
        if write:
            FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
            FEED_FILE.write_text(json.dumps(feed[:MAX_FEED], indent=1))
        print(f"card upgraded: {brief.player} -> {brief.to_club} ({brief.kind}/{brief.stage})")
        return "upgraded"
    _names, _tier = source_meta(brief.source)
    feed.insert(0, {
        "id": article["id"],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": brief.kind,
        "photo": photo,
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
        "sources": _names,
        "srcTier": _tier,
        "summary": brief.summary,
        "outlet": article["source"],
        "url": article["url"],
        "title": article["title"],
    })
    if write:
        FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
        if len(feed) > MAX_FEED:
            archive_cards(feed[MAX_FEED:])  # rolled-off cards keep their history
        FEED_FILE.write_text(json.dumps(feed[:MAX_FEED], indent=1))
    return "new"


def write_feed(feed):
    """Persist feed once after a poll; archive overflow."""
    feed[:] = prune_old_rumours(feed)
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(feed) > MAX_FEED:
        archive_cards(feed[MAX_FEED:])
    FEED_FILE.write_text(json.dumps(feed[:MAX_FEED], indent=1))
    del feed[MAX_FEED:]


def write_push_meta(subs):
    """Publish hash prefixes of the paired push endpoints for the app.

    The app hashes its own subscription endpoint and checks membership; a
    miss means iOS rotated the subscription and pushes are going nowhere —
    the app can then tell the user to re-pair instead of failing silently.
    Hash prefixes reveal nothing about the endpoints themselves.
    """
    import hashlib
    if not subs:
        return
    try:
        hashes = sorted(
            hashlib.sha256(s["endpoint"].encode()).hexdigest()[:16]
            for s in subs
        )
    except (KeyError, TypeError):
        return
    meta = json.dumps({"endpoints": hashes})
    if not PUSH_META_FILE.exists() or PUSH_META_FILE.read_text() != meta:
        PUSH_META_FILE.parent.mkdir(parents=True, exist_ok=True)
        PUSH_META_FILE.write_text(meta)


PUSH_SUBS_FILE = Path(os.environ.get("PUSH_SUBS_FILE", Path(__file__).with_name("push-subs.enc")))


def _fernet():
    key = os.environ.get("PUSH_ENC_KEY", "")
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def load_push_subs():
    """Subscription store: encrypted file in the repo (public repo — endpoints
    must not be plaintext), falling back to the legacy secret."""
    f = _fernet()
    if f and PUSH_SUBS_FILE.exists():
        try:
            return json.loads(f.decrypt(PUSH_SUBS_FILE.read_bytes()).decode())
        except Exception as e:  # noqa: BLE001
            print(f"push store decrypt failed: {e}", file=sys.stderr)
    raw = os.environ.get("PUSH_SUBSCRIPTIONS", "")
    try:
        return json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []


def save_push_subs(subs):
    f = _fernet()
    if f:
        PUSH_SUBS_FILE.write_bytes(f.encrypt(json.dumps(subs).encode()))


def collect_new_subscriptions(state):
    """Self-service re-pairing: iOS rotates push subscriptions after service
    worker updates; the user pastes the new code to the Telegram bot and this
    picks it up on the next poll — no manual secret updates.

    Only messages from the owner's chat are honoured.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return []
    offset = state.get("tg_update_offset", 0)
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getUpdates?offset={offset + 1}&timeout=0",
            timeout=30,
        ) as resp:
            updates = json.loads(resp.read().decode()).get("result", [])
    except Exception as e:  # noqa: BLE001
        print(f"getUpdates failed: {e}", file=sys.stderr)
        return []
    found = []
    for u in updates:
        state["tg_update_offset"] = max(state.get("tg_update_offset", 0), u.get("update_id", 0))
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id", "")) != str(chat_id):
            continue  # pairing codes are only accepted from the owner
        text = (msg.get("text") or "").strip()
        if not (text.startswith("{") and '"endpoint"' in text):
            continue
        try:
            sub = json.loads(text)
            if sub["endpoint"].startswith("https://") and sub["keys"]["p256dh"] and sub["keys"]["auth"]:
                found.append(sub)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return found


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


def send_web_push(article, brief, state, subs):
    """Push the card to every subscribed browser (the installed PWA).

    Subscriptions live in the PUSH_SUBSCRIPTIONS secret (JSON array) rather
    than the repo — the repo is public and endpoints shouldn't be. No-op
    until the secret and VAPID key are configured. A dead subscription
    (404/410 from the push service) triggers a Telegram re-pair alert, at
    most once per 12h — notifications must never fail silently.
    """
    pem_file = os.environ.get("VAPID_PEM_FILE", "")
    if not subs or not pem_file:
        print("web push skipped: no subscriptions or VAPID_PEM_FILE not set", file=sys.stderr)
        return False
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("pywebpush not installed; skipping web push", file=sys.stderr)
        return False
    if brief.kind == "interest":
        title = f"👀 {brief.to_club} interested in {brief.player}"
    else:
        shown = "Confirmed" if _norm(brief.stage) == "completed" else brief.stage
        title = f"⚽️ {brief.player} → {brief.to_club} · {shown}"
    payload = json.dumps({
        "title": title,
        "body": " · ".join(x for x in (brief.fee, brief.source) if x.strip() not in ("", "—")),
        "url": "./",  # tapping the notification opens the app, not the article
        # same tag per player: a stage upgrade replaces the older notification
        # (renotify makes it alert again) instead of piling up
        "tag": _norm(brief.player)[:40] if _norm(brief.player) not in ("", "—") else None,
    })
    sent, dead = 0, []
    for sub in list(subs):
        try:
            # ttl matters: the default of 0 means "deliver this instant or
            # drop" — pushes to a sleeping phone silently vanished. 24h TTL
            # lets the push service hold it until the device is reachable.
            webpush(sub, payload, vapid_private_key=pem_file,
                    vapid_claims={"sub": "mailto:yuval0156@gmail.com"},
                    ttl=86400, headers={"Urgency": "high"})
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            print(f"web push error ({code}): {e}", file=sys.stderr)
            if code in (404, 410):
                dead.append(sub["endpoint"].split("/")[2])
                subs.remove(sub)  # dead endpoint — prune from the store
                save_push_subs(subs)
    print(f"push sent to {sent} subscription(s): {title}")
    if dead:
        last = state.get("push_alert_ts", "")
        now = datetime.now(timezone.utc)
        if not last or (now - datetime.fromisoformat(last)).total_seconds() > 12 * 3600:
            try:
                send_plain_telegram(
                    f"⚠️ ShimShim: a phone push subscription is dead ({', '.join(dead)}) — "
                    "notifications are not reaching that device. Open the app → "
                    "Settings → Enable notifications and send the new code to re-pair."
                )
                state["push_alert_ts"] = now.isoformat(timespec="seconds")
            except Exception as te:  # noqa: BLE001
                print(f"push re-pair alert failed: {te}", file=sys.stderr)
    return bool(dead)


def repair_one_photo(state, feed=None):
    """Each poll, retry the photo lookup for one recent photo-less card.

    Youth players get photos as they break through; cached misses would
    otherwise freeze the crest fallback forever. One per run keeps it free.
    Returns True only when a photo was actually written (so callers can
    avoid dirtying state.json / git for no-op index bumps).
    """
    write = feed is None
    if feed is None:
        try:
            feed = json.loads(FEED_FILE.read_text())
        except Exception:  # noqa: BLE001
            return False
    cutoff = (datetime.now(timezone.utc).timestamp() - 14 * 86400)
    todo = [i for i in feed
            if not i.get("photo") and i.get("player", "").strip() not in ("", "—")
            and datetime.fromisoformat(i["ts"]).timestamp() > cutoff]
    if not todo:
        return False
    idx = state.get("photo_repair_idx", 0) % len(todo)
    card = todo[idx]
    state.setdefault("photos", {}).pop(_norm(card["player"]), None)  # allow retry
    photo = lookup_player_photo(card["player"], f"{card['from_club']}|{card['to_club']}", state)
    # Advance the cursor either way so we rotate through the backlog; only
    # persist when a photo lands (or something else dirtied state).
    state["photo_repair_idx"] = idx + 1
    if not photo:
        return False
    for i in feed:
        if i["player"] == card["player"] and not i.get("photo"):
            i["photo"] = photo
    if write:
        FEED_FILE.write_text(json.dumps(feed, indent=1))
    print(f"photo repaired: {card['player']}")
    return True


def main():
    state = load_state()
    dirty = False
    subs = load_push_subs()
    if not DRY_RUN:
        new_subs = collect_new_subscriptions(state)
        if new_subs:
            for sub in new_subs:
                subs = [s for s in subs if s["endpoint"] != sub["endpoint"]]
                subs.append(sub)
            subs = subs[-5:]  # at most a handful of devices
            save_push_subs(subs)
            dirty = True
            try:
                send_plain_telegram(
                    f"✅ ShimShim: {len(new_subs)} device(s) paired for notifications."
                )
            except Exception as e:  # noqa: BLE001
                print(f"pairing confirmation failed: {e}", file=sys.stderr)
        write_push_meta(subs)
    seen = set(state["sent"])
    deals = state["deals"]  # deal key -> highest stage rank already sent
    interest_sent = set(state["interest"])
    client = None  # lazy — idle polls with nothing to classify skip Anthropic init
    articles = []
    try:
        articles += fetch_telegram_posts(seen=seen)
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
    feed = []
    feed_dirty = False
    if not DRY_RUN and FEED_FILE.exists():
        try:
            feed = json.loads(FEED_FILE.read_text())
        except json.JSONDecodeError:
            feed = []
    # same story from several outlets or several runs: brief it once
    briefed_titles = set(state["titles"])
    # oldest first so messages arrive in chronological order
    for article in reversed(articles):
        if not article["id"] or article["id"] in seen:
            continue
        if not is_relevant(article):
            continue  # keyword prefilter — don't waste a Claude call
        if DRY_RUN:
            print(f"[dry-run] would brief: {article['title'][:90]}")
            continue
        title_key = _norm(article["title"])[:80]
        if title_key in briefed_titles:
            # duplicate headline this run — the first copy carries the story
            seen.add(article["id"])
            state["sent"].append(article["id"])
            dirty = True
            print(f"skipped (duplicate headline this run): {article['title']}")
            continue
        try:
            if client is None:
                client = anthropic.Anthropic()
            # Tiny kind gate first — most keyword false-positives stop here
            # before we pay for a full briefing extract.
            gate = classify_kind(client, article)
            if gate.kind == "interest" and not WATCHED_CLUB_RE.search(
                    _norm(f"{article['title']} {article['desc']}")):
                # Deal-keyword prefilter can admit non-watched interest (e.g.
                # "official bid" for Ipswich). No watched club ⇒ unpublishable;
                # don't spend a second Claude call extracting a dead card.
                seen.add(article["id"])
                state["sent"].append(article["id"])
                briefed_titles.add(title_key)
                state["titles"].append(title_key)
                dirty = True
                print(f"skipped (interest, no watched club): {article['title']}")
                continue
            if gate.kind == "none":
                brief = TransferBrief(
                    kind="none", stage="—", player="—", position="—", age="—",
                    from_club="—", to_club="—", fee="—", style="—", fit="—",
                    source="—", summary="—",
                )
            else:
                brief = classify_article(client, article)
            if brief.kind != "none":
                # anything that would publish gets web-verified — but not
                # before checking it isn't already carded (don't pay to
                # re-suppress duplicates)
                if brief.kind == "deal":
                    key = deal_key(brief)
                    dup = key and stage_rank(brief) <= deals.get(key, 0)
                else:
                    keys = interest_keys(brief)
                    dup = keys and all(k in interest_sent for k in keys)
                if dup:
                    seen.add(article["id"])
                    state["sent"].append(article["id"])
                    briefed_titles.add(title_key)
                    state["titles"].append(title_key)
                    dirty = True
                    print(f"skipped (already carded, pre-verify): {article['title']}")
                    continue
                if needs_web_verify(article, brief):
                    brief = verify_deal(client, article)
                else:
                    print(f"skipped verify ({brief.kind}): {article['title']}")
                # Free live-squad check — catches recycled/wrong cards that
                # slipped past classify (and cheaply corroborates verify).
                if brief.kind != "none":
                    brief = oracle_sanity_check(brief)
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
                        dirty = True
                    except Exception as te:  # noqa: BLE001
                        print(f"billing alert failed: {te}", file=sys.stderr)
                break
            print(f"claude error on '{article['title']}': {e}", file=sys.stderr)
            continue
        briefed_titles.add(title_key)
        state["titles"].append(title_key)
        dirty = True
        if state.pop("billing_alert_ts", None):
            try:  # first successful brief after an outage — all clear
                send_plain_telegram("✅ ShimShim is back: Anthropic credits restored, catching up on pending stories.")
            except Exception as te:  # noqa: BLE001
                print(f"all-clear alert failed: {te}", file=sys.stderr)
        # Mark processed regardless of verdict so we don't re-evaluate it.
        seen.add(article["id"])
        state["sent"].append(article["id"])
        if any(sep in brief.player for sep in (",", ";", " & ")):
            # "player" holding several names means a merged multi-player card
            # (a £246m "double deal" article produced one); the schema is one
            # player per card
            print(f"skipped (multi-player parse): {brief.player[:60]}")
            brief.kind = "none"
        if brief.kind == "interest" and not in_open_window(article.get("published")):
            # Transfer-window gate: a rumour only publishes when its report
            # date falls inside an open window. Between windows it's
            # speculation, not actionable news. Uses the report's own date
            # (falls back to now). Deals are never gated.
            print(f"skipped (rumour outside transfer window): {article['title']}")
            brief.kind = "none"
        if brief.kind == "interest":
            # Sanity guards the model kept violating: a club cannot pursue
            # its own player (misparsed asking-price stories produced
            # "Chelsea -> Chelsea"), and interest cards are only for the
            # watched clubs (Roma/Villa/Newcastle suitors slipped through).
            suitors = [c.strip() for c in brief.to_club.split(",") if c.strip()]
            if known_club(brief.from_club):
                suitors = [c for c in suitors if _norm_club(c) != _norm_club(brief.from_club)]
            suitors = [c for c in suitors if _norm_club(c) in WATCHED_CANON]
            if suitors:
                brief.to_club = ", ".join(suitors)
            else:
                brief.kind = "none"
        if brief.kind == "deal" and known_club(brief.from_club) and \
                _norm_club(brief.from_club) == _norm_club(brief.to_club):
            brief.kind = "none"  # same-club "transfer" is a parse error
        if brief.kind != "none":
            gate = brief_problems(brief)
            if gate:
                print(f"skipped (incomplete card: {', '.join(gate)}): {article['title']}")
                brief.kind = "none"
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
        # style/fit are deferred until a card actually publishes (Haiku).
        try:
            brief = fill_scout_lines(client, article, brief)
        except Exception as e:  # noqa: BLE001 — publish with placeholders rather than drop
            print(f"scout-lines error on '{brief.player}': {e}", file=sys.stderr)
        photos_before = dict(state.get("photos", {}))
        crests_before = dict(state.get("crests", {}))
        photo = lookup_player_photo(brief.player, f"{brief.from_club}|{brief.to_club}", state)
        ensure_club_crests(brief, state)
        if state.get("photos") != photos_before or state.get("crests") != crests_before:
            dirty = True
        result = append_feed(article, brief, photo, feed=feed)
        if result == "noop":
            print(f"skipped push (identical card): {brief.player}")
            continue
        feed_dirty = True
        try:
            if send_web_push(article, brief, state, subs):
                dirty = True
        except Exception as e:  # noqa: BLE001 — push failure must not block the feed
            print(f"web push error: {e}", file=sys.stderr)
        if TELEGRAM_CARDS:
            try:
                tg_result = send_telegram(article, brief)
                if not tg_result.get("ok"):
                    print(f"telegram error: {tg_result}", file=sys.stderr)
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

    if not DRY_RUN:
        if feed or FEED_FILE.exists():
            if not feed and FEED_FILE.exists():
                try:
                    feed = json.loads(FEED_FILE.read_text())
                except json.JSONDecodeError:
                    feed = []
            before = len(feed)
            feed = prune_old_rumours(feed)
            if len(feed) != before:
                feed_dirty = True
        if feed_dirty:
            write_feed(feed)
        rotate_windows()
        if repair_one_photo(state, feed=feed):
            feed_dirty = True
            dirty = True
            write_feed(feed)
        if dirty:
            save_state(state)
        else:
            print("state unchanged — skip write")
    print(f"done. {sent_count} briefing(s) sent, {len(articles)} scanned."
          + (" [dry run]" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
