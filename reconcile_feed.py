#!/usr/bin/env python3
"""One-off reconciliation: check every OPEN card against current reality.

Rumour and here-we-go cards can lag reality (the upgrade article was missed
or predates the journey system). Batches of open cards are fact-checked via
web search; cards whose transfers progressed are upgraded IN PLACE — forward
only, never downgraded, original timestamps kept, no notifications.
"""
import json
import os
import sys
from typing import Literal

import anthropic
from pydantic import BaseModel

from shimshim_bot import (CLAUDE_MODEL, FEED_FILE, STAGE_RANK, STATE_FILE,
                          _norm, _norm_club, deal_key, stage_rank)

BATCH = 15
MAX_BATCHES = int(os.environ.get("RECONCILE_MAX_BATCHES", "10"))


class CardStatus(BaseModel):
    index: int
    status: Literal["open", "here_we_go", "completed", "unknown"]
    to_club: str  # destination if here_we_go/completed ("" otherwise)
    fee: str      # updated fee if known ("" otherwise)
    note: str


class Reconciliation(BaseModel):
    verdicts: list[CardStatus]


def main():
    feed = json.loads(FEED_FILE.read_text())
    state = json.loads(STATE_FILE.read_text())
    open_cards = [c for c in feed
                  if c["kind"] == "interest"
                  or (c["kind"] == "deal" and c.get("stage") != "Completed")]
    print(f"{len(open_cards)} open cards")
    client = anthropic.Anthropic()
    upgraded = 0

    for bstart in range(0, min(len(open_cards), BATCH * MAX_BATCHES), BATCH):
        batch = open_cards[bstart:bstart + BATCH]
        lines = []
        for n, c in enumerate(batch):
            what = f"rumoured to {c['to_club']}" if c["kind"] == "interest" \
                else f"here-we-go to {c['to_club']}"
            lines.append(f"{n}. {c['player']} ({c['from_club']}) — {what}, "
                         f"card dated {c['ts'][:10]}")
        research = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            system=(
                "Today is deadline day of the summer 2026 transfer window. "
                "For each numbered player below, use web search to determine "
                "the CURRENT status of their move: still just rumoured/open, "
                "at here-we-go/agreement stage, or officially COMPLETED "
                "(club announcement) — and to WHICH club (it may differ from "
                "the card). Batch players into searches where sensible; you "
                "have at most 10 searches. End with a numbered findings list "
                "— one line per player: status, destination club, fee if "
                "known. Unverifiable players: status unknown. Never end "
                "without the findings list."
            ),
            messages=[{"role": "user", "content": "\n".join(lines)}],
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 10}],
        )
        notes = "\n".join(b.text for b in research.content if b.type == "text")
        resp = client.messages.parse(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=(
                "Convert the findings into a verdict per numbered card. "
                "status 'completed' or 'here_we_go' ONLY when the findings "
                "state it with a destination club; otherwise 'open' (or "
                "'unknown' if unverifiable)."
            ),
            messages=[{"role": "user", "content": f"Cards:\n" + "\n".join(lines)
                       + f"\n\nFindings:\n{notes}"}],
            output_format=Reconciliation,
        )
        for v in resp.parsed_output.verdicts:
            if not (0 <= v.index < len(batch)):
                continue
            c = batch[v.index]
            if v.status not in ("here_we_go", "completed") or not v.to_club.strip():
                print(f"[{v.status:9}] {c['player']}")
                continue
            new_stage = "Completed" if v.status == "completed" else "Here we go"
            cur_rank = STAGE_RANK.get(_norm(c.get("stage", "")), 0) if c["kind"] == "deal" else 0
            if STAGE_RANK[_norm(new_stage)] <= cur_rank:
                print(f"[keep     ] {c['player']} (already {c.get('stage')})")
                continue
            c["kind"] = "deal"
            c["stage"] = new_stage
            c["to_club"] = v.to_club.strip()
            if v.fee.strip():
                c["fee"] = v.fee.strip()
            upgraded += 1
            key = f"{_norm(c['player']).split()[-1]} -> {_norm_club(v.to_club)}"
            rank = STAGE_RANK[_norm(new_stage)]
            if state["deals"].get(key, 0) < rank:
                state["deals"][key] = rank
            print(f"[UPGRADED ] {c['player']} -> {v.to_club} ({new_stage}) — {v.note[:70]}")

    FEED_FILE.write_text(json.dumps(feed, indent=1))
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"done: {upgraded} cards upgraded")


if __name__ == "__main__":
    main()
