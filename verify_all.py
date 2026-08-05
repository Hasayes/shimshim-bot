#!/usr/bin/env python3
"""Full-feed solidity audit: verify EVERY card against reality, both ways.

Checks each card's stage (over-staged Confirmed without an official
announcement gets downgraded; stale cards get upgraded), clubs, and fee.
Corrections applied only on explicit evidence; everything logged.
"""
import json
import sys
from typing import Literal

import anthropic
from pydantic import BaseModel

from shimshim_bot import (CLAUDE_MODEL, FEED_FILE, STAGE_RANK, STATE_FILE,
                          _norm, _norm_club)

BATCH = 15


class CardCheck(BaseModel):
    index: int
    verdict: Literal["ok", "wrong_stage", "wrong_clubs", "not_real", "unknown"]
    correct_stage: str    # "Rumour", "Here we go", "Completed" ("" if n/a)
    correct_to_club: str  # "" if unchanged
    correct_fee: str      # "" if unchanged
    note: str


class FullCheck(BaseModel):
    verdicts: list[CardCheck]


def main():
    feed = json.loads(FEED_FILE.read_text())
    state = json.loads(STATE_FILE.read_text())
    client = anthropic.Anthropic()
    fixed = flagged = 0

    for b in range(0, len(feed), BATCH):
        batch = feed[b:b + BATCH]
        lines = []
        for n, c in enumerate(batch):
            what = f"RUMOUR: {c['to_club']} interested" if c["kind"] == "interest" \
                else f"{c.get('stage', '?').upper()}: move to {c['to_club']}"
            lines.append(f"{n}. {c['player']} (from {c['from_club']}) — {what}; "
                         f"fee {c.get('fee', '—')}; card dated {c['ts'][:10]}")
        research = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            system=(
                "The summer 2026 window just closed. Audit these transfer "
                "cards with web search: for each, is the stated status TRUE "
                "today? Critical distinctions: COMPLETED requires an "
                "official club announcement — an agreement/'here we go' is "
                "NOT completed. Also catch: wrong destination club, deals "
                "that collapsed, clearly wrong fees. Batch searches "
                "sensibly; max 10 searches. End with a numbered findings "
                "list, one line per card: true status + destination + fee "
                "if known, or 'could not verify'. Never end without it."
            ),
            messages=[{"role": "user", "content": "\n".join(lines)}],
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 10}],
        )
        notes = "\n".join(x.text for x in research.content if x.type == "text")
        resp = client.messages.parse(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=(
                "Convert the findings into a verdict per numbered card. "
                "'wrong_stage'/'wrong_clubs'/'not_real' ONLY on explicit "
                "evidence in the findings; unverified cards are 'unknown'; "
                "cards the findings support are 'ok'. correct_stage uses "
                "'Rumour', 'Here we go' or 'Completed'."
            ),
            messages=[{"role": "user", "content": "Cards:\n" + "\n".join(lines)
                       + f"\n\nFindings:\n{notes}"}],
            output_format=FullCheck,
        )
        for v in resp.parsed_output.verdicts:
            if not (0 <= v.index < len(batch)):
                continue
            c = batch[v.index]
            if v.verdict in ("ok", "unknown"):
                continue
            flagged += 1
            print(f"[{v.verdict}] {c['player']} ({c.get('stage') or c['kind']}"
                  f" -> {c['to_club']}): {v.note[:100]}")
            if v.verdict == "wrong_stage" and v.correct_stage:
                if v.correct_stage == "Rumour":
                    c["kind"] = "interest"
                    c["stage"] = "—"
                else:
                    c["kind"] = "deal"
                    c["stage"] = v.correct_stage
                key = f"{_norm(c['player']).split()[-1]} -> {_norm_club(c['to_club'])}"
                state["deals"][key] = STAGE_RANK.get(_norm(v.correct_stage), 1) \
                    if v.correct_stage != "Rumour" else 0
                fixed += 1
            elif v.verdict == "wrong_clubs" and v.correct_to_club:
                c["to_club"] = v.correct_to_club
                fixed += 1
            if v.correct_fee.strip():
                c["fee"] = v.correct_fee.strip()

    FEED_FILE.write_text(json.dumps(feed, indent=1))
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"done: {flagged} flagged, {fixed} fixed")


if __name__ == "__main__":
    main()
