#!/usr/bin/env python3
"""Weekly self-audit: fact-check the last week's cards against live search.

One research call (web search) covering all recent deal cards + a structured
verdict pass. Problems are reported to Telegram; nothing is auto-removed —
a human (or Claude in a session) decides. Runs from audit.yml every Sunday.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Literal

import base64
import urllib.request

import anthropic
from pydantic import BaseModel

from shimshim_bot import CLAUDE_MODEL, FEED_FILE, send_plain_telegram

AUDIT_DAYS = int(os.environ.get("AUDIT_DAYS", "7"))
MAX_CARDS = int(os.environ.get("AUDIT_MAX_CARDS", "25"))


class PhotoVerdict(BaseModel):
    index: int
    plausible: bool
    note: str  # why it's suspicious ("" when plausible)


class PhotoAudit(BaseModel):
    verdicts: list[PhotoVerdict]


def visual_photo_check(client, cards):
    """Claude eyeballs the week's new photos like a human reviewer would.

    Sends the actual images with the card facts and asks for plausibility:
    right sport, age matches the card, kit consistent with the clubs. This
    is the check that caught a father's photo on his son's card.
    """
    subjects = []
    seen_urls = set()
    for c in cards:
        url = c.get("photo", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        subjects.append(c)
    subjects = subjects[:20]
    if not subjects:
        return []

    content = []
    lines = []
    loaded = []
    for c in subjects:
        try:
            req = urllib.request.Request(c["photo"], headers={"User-Agent": "shimshim-audit/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except Exception as e:  # noqa: BLE001
            print(f"photo fetch failed for {c['player']}: {e}", file=sys.stderr)
            continue
        media = "image/png" if raw[:8].startswith(b"\x89PNG") else "image/jpeg"
        n = len(loaded)
        loaded.append(c)
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media,
            "data": base64.standard_b64encode(raw).decode()}})
        age = c.get("age", "") or "unknown"
        lines.append(f"Image {n}: claimed to be {c['player']} "
                     f"(age {age}, {c.get('position') or 'position unknown'}, "
                     f"clubs: {c['from_club']} / {c['to_club']})")
    if not loaded:
        return []
    content.append({"type": "text", "text": (
        "You are auditing a football news app's player photos, in the order "
        "given:\n" + "\n".join(lines) + "\n\n"
        "For each image judge PLAUSIBILITY, flagging only real red flags:\n"
        "- not a football player (different sport, non-athlete, mascot)\n"
        "- apparent age wildly inconsistent with the claimed age (e.g. a "
        "40-something for a listed 19-year-old)\n"
        "- visible kit/branding clearly belonging to none of the listed "
        "clubs' colours (allow national teams, training wear, old clubs)\n"
        "- obviously the wrong famous person\n"
        "Unremarkable or ambiguous photos are plausible — only flag clear "
        "mismatches.")})
    resp = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": content}],
        output_format=PhotoAudit,
    )
    flagged = []
    for v in resp.parsed_output.verdicts:
        if not v.plausible and 0 <= v.index < len(loaded):
            c = loaded[v.index]
            flagged.append(f"• {c['player']} ({c['from_club']} → {c['to_club']}): {v.note}")
            print(f"[PHOTO?] {c['player']} — {v.note}")
        elif 0 <= v.index < len(loaded):
            print(f"[photo ok] {loaded[v.index]['player']}")
    return flagged


class Verdict(BaseModel):
    index: int
    verdict: Literal["ok", "wrong", "unsure"]
    note: str  # one sentence: what's wrong / uncertain ("" when ok)


class Audit(BaseModel):
    verdicts: list[Verdict]


def main():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=AUDIT_DAYS)).isoformat()
    feed = json.loads(FEED_FILE.read_text())

    # structural lint over the WHOLE feed first — free, catches broken cards
    # (empty player, deal without clubs) regardless of age
    from shimshim_bot import TransferBrief, brief_problems
    broken = []
    for c in feed:
        b = TransferBrief(kind=c["kind"] if c["kind"] in ("deal", "interest") else "none",
                          stage=c.get("stage", "—"), player=c.get("player", ""),
                          position="", age="", from_club=c.get("from_club", ""),
                          to_club=c.get("to_club", ""), fee="", style="", fit="", source="")
        probs = brief_problems(b) if b.kind != "none" else []
        if probs:
            broken.append(f"• {c.get('player') or '(no player)'} ({c.get('ts', '')[:10]}): {', '.join(probs)}")
    if broken:
        send_plain_telegram("🧱 ShimShim structural audit: broken cards in the feed:\n"
                            + "\n".join(broken[:15])
                            + "\nAsk Claude to clean these up.")
        print(f"structural: {len(broken)} broken card(s) flagged")
    cards = [i for i in feed if i["ts"] >= cutoff][:MAX_CARDS]
    if not cards:
        print("nothing to audit")
        return
    client = anthropic.Anthropic()

    photo_flags = visual_photo_check(client, [c for c in cards if c.get("photo")])
    if photo_flags and os.environ.get("AUDIT_DRY", "0") != "1":
        send_plain_telegram("🖼 ShimShim photo audit — these faces look wrong:\n"
                            + "\n".join(photo_flags[:10])
                            + "\nAsk Claude to verify and fix.")

    lines = []
    for n, c in enumerate(cards):
        what = ("interest from " + c["to_club"]) if c["kind"] == "interest" \
            else f"transfer to {c['to_club']} ({c['stage']})"
        lines.append(f"{n}. {c['player']} — {c['from_club']}: {what}, fee {c['fee']}, "
                     f"published {c['ts'][:10]}")
    listing = "\n".join(lines)

    research = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        system=(
            "You audit a football transfer news feed. Today's date matters — "
            "use web search to spot-check the listed cards for: transfers that "
            "never actually happened or collapsed, wrong clubs or direction, "
            "players at a different club than stated, clearly wrong fees, or "
            "recycled old-season stories. You have at most 10 searches — "
            "prioritise the most suspicious-looking entries and batch-check "
            "the rest with broad searches. End with bullet findings per card "
            "number: OK / WRONG (why) / UNSURE (why). Never end without the "
            "findings list."
        ),
        messages=[{"role": "user", "content": listing}],
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 10}],
    )
    notes = "\n".join(b.text for b in research.content if b.type == "text")

    resp = client.messages.parse(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=(
            "Convert the audit findings into verdicts for each numbered card. "
            "verdict 'wrong' ONLY when the findings cite concrete evidence "
            "that the card misstates reality (deal never happened, wrong "
            "clubs, recycled old story, clearly wrong fee). 'unsure' ONLY "
            "when the findings raise a specific substantive doubt. A card "
            "the audit simply didn't check, or found nothing against, is "
            "'ok' — lack of confirmation is NOT a problem."
        ),
        messages=[{"role": "user", "content": f"Cards:\n{listing}\n\nFindings:\n{notes}"}],
        output_format=Audit,
    )
    verdicts = resp.parsed_output.verdicts
    for v in verdicts:  # full transparency in the run log
        if 0 <= v.index < len(cards):
            c = cards[v.index]
            print(f"[{v.verdict.upper():6}] {c['player']} ({c['from_club']} -> {c['to_club']})"
                  + (f" — {v.note}" if v.note else ""))
    # alert only on evidence-backed problems; 'unsure' stays in the log
    problems = [v for v in verdicts if v.verdict == "wrong" and 0 <= v.index < len(cards)]
    print(f"audited {len(cards)} cards, {len(problems)} wrong")
    if os.environ.get("AUDIT_DRY", "0") == "1":
        print("[dry] telegram alert suppressed")
        return
    if problems:
        msg_lines = ["🔎 ShimShim weekly audit flagged some cards:"]
        for v in problems:
            c = cards[v.index]
            msg_lines.append(f"• {c['player']} ({c['from_club']} → {c['to_club']}): "
                             f"{v.verdict.upper()} — {v.note}")
        msg_lines.append("Ask Claude to investigate and clean these up.")
        send_plain_telegram("\n".join(msg_lines)[:4000])
    else:
        print("all cards check out — no alert sent")


if __name__ == "__main__":
    main()
