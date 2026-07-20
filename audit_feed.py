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

import anthropic
from pydantic import BaseModel

from shimshim_bot import CLAUDE_MODEL, FEED_FILE, send_plain_telegram

AUDIT_DAYS = int(os.environ.get("AUDIT_DAYS", "7"))
MAX_CARDS = int(os.environ.get("AUDIT_MAX_CARDS", "25"))


class Verdict(BaseModel):
    index: int
    verdict: Literal["ok", "wrong", "unsure"]
    note: str  # one sentence: what's wrong / uncertain ("" when ok)


class Audit(BaseModel):
    verdicts: list[Verdict]


def main():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=AUDIT_DAYS)).isoformat()
    feed = json.loads(FEED_FILE.read_text())
    cards = [i for i in feed if i["ts"] >= cutoff][:MAX_CARDS]
    if not cards:
        print("nothing to audit")
        return

    lines = []
    for n, c in enumerate(cards):
        what = ("interest from " + c["to_club"]) if c["kind"] == "interest" \
            else f"transfer to {c['to_club']} ({c['stage']})"
        lines.append(f"{n}. {c['player']} — {c['from_club']}: {what}, fee {c['fee']}, "
                     f"published {c['ts'][:10]}")
    listing = "\n".join(lines)

    client = anthropic.Anthropic()
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
            "verdict 'wrong' only when the findings give concrete evidence; "
            "'unsure' when the findings flag doubt; otherwise 'ok'."
        ),
        messages=[{"role": "user", "content": f"Cards:\n{listing}\n\nFindings:\n{notes}"}],
        output_format=Audit,
    )
    verdicts = resp.parsed_output.verdicts
    problems = [v for v in verdicts if v.verdict != "ok" and 0 <= v.index < len(cards)]
    print(f"audited {len(cards)} cards, {len(problems)} flagged")
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
