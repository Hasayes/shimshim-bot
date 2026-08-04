#!/usr/bin/env python3
"""One-off: generate news summaries for existing cards (batched)."""
import json
import sys

import anthropic
from pydantic import BaseModel

from shimshim_bot import CLAUDE_MODEL, FEED_FILE

BATCH = 25


class Summary(BaseModel):
    index: int
    summary: str


class Summaries(BaseModel):
    items: list[Summary]


def main():
    feed = json.loads(FEED_FILE.read_text())
    todo = [c for c in feed if not (c.get("summary") or "").strip()]
    print(f"{len(todo)} cards need summaries")
    client = anthropic.Anthropic()
    done = 0
    for b in range(0, len(todo), BATCH):
        batch = todo[b:b + BATCH]
        lines = []
        for n, c in enumerate(batch):
            what = f"interest from {c['to_club']}" if c["kind"] == "interest" \
                else f"{c.get('stage', 'deal')} move to {c['to_club']}"
            lines.append(f"{n}. {c['player']} ({c['from_club']}): {what}; "
                         f"fee {c.get('fee', '—')}; dated {c['ts'][:10]}; "
                         f"headline: \"{(c.get('title') or '')[:110]}\"")
        resp = client.messages.parse(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=(
                "For each numbered transfer card, write a 1-2 sentence "
                "factual news summary using ONLY the facts given (player, "
                "clubs, stage, fee, headline). State what happened plainly; "
                "no opinions, no speculation, no invented details."
            ),
            messages=[{"role": "user", "content": "\n".join(lines)}],
            output_format=Summaries,
        )
        for s in resp.parsed_output.items:
            if 0 <= s.index < len(batch) and s.summary.strip():
                batch[s.index]["summary"] = s.summary.strip()
                done += 1
    FEED_FILE.write_text(json.dumps(feed, indent=1))
    print(f"done: {done} summaries written")


if __name__ == "__main__":
    main()
